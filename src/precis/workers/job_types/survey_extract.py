"""``survey_extract`` + ``measure_accept`` — the survey/review numeric
extraction pass (docs/backlog/measures-substrate.md, "Next slice").

``survey_extract`` walks one paper's chunks in slices. Each live chunk
is rendered with ``[n]`` anchors (:mod:`precis.extract.anchors`),
prompted to the local model on the served-OSS lane (``placement=
"local"``, ``Tier.BIG`` — the local reader on castor; the chain is the
steerable ``llm.op.survey_extract`` op, and the dispatch refuses up
front when no chain rung can satisfy the placement), and the reply is
turned into checked runs by :func:`precis.extract.proposals.build_runs`.
Good runs land through ``store.insert_measure`` with
``meta.extraction_run`` — the batch token the acceptance step samples
by. A re-run of the same paper re-derives the same literals on the same
spans and the store's duplicate refusal turns them into skips, so the
pass is idempotent.

The slice boundary is the pacing: after ``chunks_per_slice`` chunks the
job parks on a ``Yield`` and waits out a full coordinator claim cycle —
minutes per slice on a busy worker, which priced a 634-chunk paper at
~10 h on the castor pilot (jo504632). That park only exists to throttle
a remote/paid endpoint, so it is rung-aware: once a routed call lands
``local`` (no external rate limit, no metered cost) the dispatch skips
the inter-chunk park and runs the paper to completion — a crash then
re-runs the paper, cheap under the idempotent dup-skip. A remote or
unknown placement keeps the per-slice park.

When every chunk is read the job spawns one ``measure_accept`` child
and waits on it. ``measure_accept`` draws the deterministic stratified
sample (:mod:`precis.extract.accept`), has a frontier-tier model review
each sampled row against its anchored span, records the verdicts on the
``reviews`` ledger, and marks the batch accepted or failed by the
Wilson rule (upper edge of the error-rate interval ≤ the bar, default
5% at the one-sided 95% quantile). The verdict rides
``refs.meta.acceptance`` on the accept job and comes back into the
parent's summary.

``measure_accept`` is also a standalone job_type — re-accepting a batch
(more samples, a different bar, a new frontier model) never re-runs the
extraction. The batch is the ``extraction_run`` token, not the paper
(corpus finding 1: a per-paper batch almost never reaches n=52, the
smallest zero-error sample that passes the bar). Given a token without
``paper`` the batch spans every paper stamped with it and the sample
stratifies by paper × measurand; ``paper`` only narrows the batch to
that paper's rows. Each verdict records its coverage — the papers in
the sampling frame plus ``max_id``, the frame's row-id high-water
mark, both frozen at the draw — and store-side
``accepted_runs`` resolves a token to the latest *counting* verdict per
covered paper (only a ``bar`` at or under and a ``z`` at or above the
defaults counts; a pre-coverage verdict grants nothing).
``best_measure``'s
``accepted_only`` keeps a token row only when its paper's governing
verdict passed, the row id sits under the mark, and the row keeps its
anchor. A run-level accept is refused while ``survey_extract`` jobs
still carry the token — sequence order is not commit order, so a row
can hold an id under the mark and commit after the frame read.

Castor-trial knobs on the extract pass: ``params.measurands`` narrows
the reader vocabulary to the named taxa plus their descendants (the
default is the whole ``measurand_root`` subtree); chunks in
bibliography/admin sections or with non-prose/table kinds are skipped
unless ``params.include_all_sections``; a ``paused`` router result parks
the slice rather than spending the consecutive-error budget — a
saturated local slot is not a down lane — but only
``PRECIS_SURVEY_EXTRACT_MAX_PAUSES`` (default 24) consecutive pauses are
tolerated before the job fails with the saturation reason instead of
parking forever (gr508162). Every park annotates
``wake_when.payload.why`` so the wake_runner's re-queue audit names
what the slice waits on. ``stats`` carries per-reason
drop counters (``drop:<reason>``, ``unparseable_reply``, ``truncated``,
``write_refused``, ``flag:<name>``, ``paused``) and every escalation
carries ``chunk_kind`` plus ``stop_reason`` on reply failures.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.extract import accept as _accept
from precis.extract import prompt as _prompt
from precis.extract import recipes as _recipes
from precis.extract.proposals import (
    RunProposal,
    VocabEntry,
    build_runs,
    vocab_entry,
)
from precis.extract.tables import (
    assign_captions,
    parse_table,
    run_headers,
    table_runs,
    table_sig,
)
from precis.utils import handle_registry
from precis.utils.env import env_int
from precis.utils.glyph_health import glyph_suspected
from precis.utils.llm.router import (
    LlmRequest,
    Tier,
    placement_refusal,
    resolve_model,
    route,
    tier_from_str,
)
from precis.workers.executors._common import TERMINAL
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

_ACTOR = "survey-extract"
_ESCALATION_CAP = 200

#: Router ``stop_reason`` values meaning the reply hit the token cap.
_TRUNCATION_STOPS = frozenset({"length", "max_tokens"})

#: The chunk kinds the reader may see — body prose and tables carry a
#: paper's numbers; cards, headings, captions, equations and bibliography
#: rows don't (castor trial review finding 4).
_EXTRACT_CHUNK_KINDS = frozenset({"paragraph", "table"})

#: ``section_path`` tails that carry no first-party results — matched
#: case-insensitively, leading section numbering allowed.
_SKIP_SECTION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s*)?"
    r"(?:references|bibliography|author contributions|acknowledg(e)?ments?)"
    r"\.?\s*$",
    re.IGNORECASE,
)


# ── helpers shared by both job types ────────────────────────────────


def _paper_id(store: Store, raw: Any) -> int | None:
    """``paper`` param → a live paper ref_id (int or ``pa…`` slug)."""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (raw,),
            ).fetchone()
        return int(raw) if row else None
    if isinstance(raw, str):
        text = raw.strip()
        if text.isdigit():
            return _paper_id(store, int(text))
        resolved = store.resolve_handle(text)
        if resolved is not None:
            return int(resolved.ref_id)
    return None


def _paper_title(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _paper_glyph_flag(store: Store, ref_id: int) -> bool:
    """The paper's ingest glyph-health flag — a font whose encoding lies
    corrupts numbers/units in the stored text (gr228652); write refusals
    on a flagged paper are named so in the escalation (gr511458)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return glyph_suspected(row[0] if row else None)


def _chunks(store: Store, paper: int) -> list[dict[str, Any]]:
    """The paper's live body chunks, ord order."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord, chunk_kind, text, section_path "
            "  FROM chunks WHERE ref_id = %s AND ord >= 0 "
            "  AND retired_at IS NULL ORDER BY ord",
            (paper,),
        ).fetchall()
    out = []
    for cid, ord_, kind, text, section in rows:
        sec = None
        if isinstance(section, list) and section:
            sec = str(section[-1])
        out.append(
            {
                "chunk_id": int(cid),
                "ord": int(ord_),
                "chunk_kind": str(kind),
                "text": str(text or ""),
                "section": sec,
            }
        )
    return out


def _measurand_root(store: Store, raw: Any) -> int | None:
    """``measurand_root`` param or the 'measurand' start node's ref_id."""
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'slug' = 'measurand' "
            "ORDER BY ref_id LIMIT 1"
        ).fetchone()
    return int(row[0]) if row else None


def _measurand_allow(store: Store, raw: Any) -> list[int] | None:
    """``measurands`` param → resolved live taxon ref_ids, ``None`` when
    unset. Entries are taxon slugs, ``tn<id>`` handles or ref_ids (a
    digit string counts as an id); anything that is not a live taxon
    refuses the job up front."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise BadInput(
            f"survey_extract: params.measurands must be a list, got {raw!r}",
            next="pass measurands=[<taxon slug, tn<id> or ref_id>, …]",
        )
    if not raw:
        return None
    want_ids: list[int] = []
    want_slugs: list[str] = []
    bad: list[str] = []
    for item in raw:
        if isinstance(item, bool):
            bad.append(repr(item))
        elif isinstance(item, int):
            want_ids.append(item)
        elif isinstance(item, str):
            text = item.strip()
            handle = handle_registry.parse(text)
            if text.isdigit():
                want_ids.append(int(text))
            elif handle is not None:
                if handle[0] == "taxon" and not handle[1]:
                    want_ids.append(handle[2])
                else:
                    bad.append(repr(item))
            elif text:
                want_slugs.append(text)
            else:
                bad.append(repr(item))
        else:
            bad.append(repr(item))
    if bad:
        raise BadInput(
            "survey_extract: params.measurands entries must be taxon slugs, "
            f"tn<id> handles or ids, got {bad}",
            next="pass measurands=[<taxon slug, tn<id> or ref_id>, …]",
        )
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'slug' FROM refs "
            "WHERE kind = 'taxon' AND retired_at IS NULL "
            "AND (ref_id = ANY(%s) OR meta->>'slug' = ANY(%s))",
            (want_ids, want_slugs),
        ).fetchall()
    found_ids = {int(r[0]) for r in rows}
    found_slugs = {str(r[1]) for r in rows if r[1]}
    missing = [str(i) for i in want_ids if i not in found_ids] + [
        s for s in want_slugs if s not in found_slugs
    ]
    if missing:
        raise BadInput(
            f"survey_extract: unknown measurand(s) {missing}",
            next=(
                "pass live taxon slugs, tn<id> handles or ref_ids — "
                "search(kind='taxon', q='<name>') lists them"
            ),
        )
    return sorted(found_ids)


def _vocab(
    store: Store, root: int | None, allow: list[int] | None = None
) -> dict[int, VocabEntry]:
    """The measurand vocabulary the reader may name: ``root`` plus all
    its descendants; when ``allow`` (the resolved ``measurands`` param)
    is set, only those taxa plus their own descendants."""
    seeds = allow if allow else ([root] if root is not None else [])
    if not seeds:
        return {}
    ids = list(seeds)
    for seed in seeds:
        try:
            ids += [rid for rid, _depth, _axis in store.taxon_descendants(seed)]
        except Exception:
            log.debug("survey_extract: taxon_descendants(%s) failed", seed)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta, title FROM refs WHERE ref_id = ANY(%s) "
            "AND retired_at IS NULL",
            (ids,),
        ).fetchall()
    out: dict[int, VocabEntry] = {}
    for rid, meta, title in rows:
        m = dict(meta or {})
        if not m.get("name"):
            m["name"] = str(title or "")
        out[int(rid)] = vocab_entry(int(rid), m)
    return out


def _result_field(results: Any, location: str, key: str) -> Any:
    """``results[i][key]`` for a proposal's ``results[i]`` location —
    ``None`` when it cannot be resolved."""
    if not isinstance(results, list) or not location.startswith("results["):
        return None
    try:
        i = int(location[len("results[") : -1])
    except ValueError:
        return None
    if not 0 <= i < len(results):
        return None
    raw = results[i]
    return raw.get(key) if isinstance(raw, dict) else None


def _skippable(chunk: dict[str, Any], kinds: frozenset[str]) -> bool:
    """A chunk the reader pass skips by policy: a kind outside the job's
    ``chunk_kinds`` selection, or a ``section_path`` tail naming a
    bibliography / admin section."""
    if chunk["chunk_kind"] not in kinds:
        return True
    section = chunk.get("section")
    return isinstance(section, str) and bool(_SKIP_SECTION_RE.match(section))


def _escalate(bucket: list[dict[str, Any]], entry: dict[str, Any]) -> None:
    if len(bucket) < _ESCALATION_CAP:
        bucket.append(entry)


def _chunk_kinds_sel(store: Store, raw: Any) -> frozenset[str]:
    """``params.chunk_kinds`` → the chunk kinds the reader sees. Defaults
    to ``_EXTRACT_CHUNK_KINDS``; an entry the ``chunk_kinds`` table
    doesn't know refuses the job up front (a typo like ``'tabel'`` would
    otherwise silently extract nothing)."""
    if raw is None:
        return _EXTRACT_CHUNK_KINDS
    if (
        not isinstance(raw, (list, tuple))
        or not raw
        or not all(isinstance(k, str) and k.strip() for k in raw)
    ):
        raise BadInput(
            f"survey_extract: params.chunk_kinds must be a non-empty list "
            f"of chunk_kind slugs, got {raw!r}",
            next="pass chunk_kinds=['paragraph', 'table'] (or omit it)",
        )
    want = [k.strip() for k in raw]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT slug FROM chunk_kinds WHERE slug = ANY(%s)", (want,)
        ).fetchall()
    known = {str(r[0]) for r in rows}
    missing = [k for k in want if k not in known]
    if missing:
        raise BadInput(
            f"survey_extract: unknown chunk_kind(s) {missing}",
            next="pass chunk_kinds entries the chunk_kinds table knows",
            options=sorted(known | set(_EXTRACT_CHUNK_KINDS)),
        )
    return frozenset(want)


#: A glyph-lie repair for a refused unit: on a ``glyph_suspected`` paper a
#: font can print a superscript minus as ``K`` — ``'W mK^2'`` is ``'W m-2'``
#: (gr512368). Only ``K^<digits>`` is touched: a bare ``K`` or ``K-1`` is a
#: real kelvin and stays.
def _glyph_repair_unit(unit: object) -> str | None:
    if not isinstance(unit, str):
        return None
    repaired = re.sub(r"K\^(\d+)", r"-\1", unit)
    return repaired if repaired != unit else None


def _wake_s(seconds: int = 1, why: str | None = None) -> WakeWhen:
    payload: dict[str, Any] = {"ts": int(time.time()) + seconds}
    if why:
        payload["why"] = why
    return WakeWhen("at_time", payload)


def _retry_wake(result: Any, why: str | None = None) -> WakeWhen:
    retry_at = getattr(result, "retry_at", None)
    if retry_at is not None:
        return WakeWhen(
            "at_time",
            {"ts": int(retry_at.timestamp()), **({"why": why} if why else {})},
        )
    return _wake_s(300, why=why)


# ── survey_extract ──────────────────────────────────────────────────

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": ["integer", "string"]},
        "model": {"type": ["string", "null"]},
        "tier": {"type": ["string", "null"]},
        "chunks_per_slice": {"type": ["integer", "null"]},
        "min_chars": {"type": ["integer", "null"]},
        "measurand_root": {"type": ["integer", "null"]},
        "measurands": {
            "type": ["array", "null"],
            "items": {"type": ["integer", "string"]},
        },
        "chunk_kinds": {
            "type": ["array", "null"],
            "items": {"type": "string"},
        },
        "include_all_sections": {"type": ["boolean", "null"]},
        "run_token": {"type": ["string", "null"]},
        "skip_accept": {"type": ["boolean", "null"]},
        "accept_model": {"type": ["string", "null"]},
        "accept_tier": {"type": ["string", "null"]},
        "sample_n": {"type": ["integer", "null"]},
        "bar": {"type": ["number", "null"]},
    },
    "required": ["paper"],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"coordinator"})
REQUIRES: frozenset[str] = frozenset()
DESCRIPTION = (
    "Survey/review numeric extraction over one paper's chunks — local "
    "reader on the served-OSS lane, anchored measure rows, then a "
    "spawned measure_accept child samples the batch (Wilson rule)."
)


def _chunks_per_slice(params: dict[str, Any]) -> int:
    raw = params.get("chunks_per_slice")
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 200:
        return raw
    return env_int("PRECIS_SURVEY_EXTRACT_CHUNKS_PER_SLICE", 8, lo=1, hi=200)


def _min_chars(params: dict[str, Any]) -> int:
    raw = params.get("min_chars")
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 4000:
        return raw
    return 60


def _max_extract_errors() -> int:
    """Consecutive LLM-error slices tolerated before the job fails."""
    return env_int("PRECIS_SURVEY_EXTRACT_MAX_ERRORS", 10, lo=1, hi=100)


def _max_pauses() -> int:
    """Consecutive paused slices tolerated before the job gives up.

    A ``paused`` router answer means the lane is saturated, not down —
    the job parks ~5 min and retries. Unbounded, that loop parks forever
    with no progress and no verdict (jo504632's last hours, gr508162).
    24 parks ≈ 2h of continuous saturation before failing with the
    reason on the record."""
    return env_int("PRECIS_SURVEY_EXTRACT_MAX_PAUSES", 24, lo=1, hi=1000)


def _fresh_stats() -> dict[str, int]:
    return {
        "chunks": 0,
        "calls": 0,
        "proposals": 0,
        "written": 0,
        "skipped_dup": 0,
        "skipped_short": 0,
        "skipped_section": 0,
        "dropped": 0,
        "table:recipe": 0,
        "table:fallback": 0,
        "table:no_results": 0,
        "table:ragged": 0,
        "table:reader_skip": 0,
        "table:recipe_kept": 0,
        "drop:bad_shape": 0,
        "drop:literal_not_in_span": 0,
        "drop:unknown_measurand": 0,
        "drop:unresolvable_anchor": 0,
        "drop:no_fitting_measurand": 0,
        "drop:cited_context": 0,
        "drop:unsupported_bound": 0,
        "drop:bound_direction_mismatch": 0,
        "drop:unsupported_sign": 0,
        "drop:uncertainty_overflow": 0,
        "unparseable_reply": 0,
        "empty_result": 0,
        "truncated": 0,
        "write_refused": 0,
        "value_form_inferred": 0,
        "paused": 0,
        "llm_errors": 0,
        "consecutive_errors": 0,
    }


def _routed(
    req: LlmRequest,
    *,
    chunk: dict[str, Any],
    stats: dict[str, int],
    escalations: list[dict[str, Any]],
) -> tuple[str | None, str | None, Any]:
    """``route(req)`` plus the paused/exception/error bookkeeping the
    reader and recipe calls share — ``(outcome, placement, result)`` on
    ``_process_chunk``'s contract."""
    stats["calls"] += 1
    try:
        result = route(req)
    except Exception as exc:
        stats["llm_errors"] += 1
        stats["consecutive_errors"] += 1
        _escalate(
            escalations,
            {
                "kind": "llm_exception",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "detail": str(exc)[:200],
            },
        )
        return (
            "failed" if stats["consecutive_errors"] >= _max_extract_errors() else None,
            None,
            None,
        )
    placement = getattr(result, "placement", None)
    if getattr(result, "paused", False):
        # A saturated local slot is a wait, not an error — it must not
        # burn the consecutive-error budget that fails the job on a
        # genuinely down lane.
        stats["paused"] += 1
        return "paused", placement, result
    if getattr(result, "error", None) or not getattr(result, "text", ""):
        stats["llm_errors"] += 1
        stats["consecutive_errors"] += 1
        _escalate(
            escalations,
            {
                "kind": "llm_error",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "stop_reason": getattr(result, "stop_reason", None),
                "detail": str(getattr(result, "error", "empty reply"))[:200],
            },
        )
        return (
            "failed" if stats["consecutive_errors"] >= _max_extract_errors() else None,
            placement,
            result,
        )
    stats["consecutive_errors"] = 0
    return None, placement, result


def _record_drops(
    drops: list[dict[str, Any]],
    *,
    chunk: dict[str, Any],
    stats: dict[str, int],
    escalations: list[dict[str, Any]],
) -> None:
    """The ``dropped`` / ``drop:<reason>`` counters and one escalation
    per refused element — shared by ``build_runs`` and ``expand_recipe``
    drops, which carry the same ``location``/``reason``/``detail`` shape."""
    for drop in drops:
        stats["dropped"] += 1
        reason = str(drop.get("reason") or "unknown")
        stats[f"drop:{reason}"] = stats.get(f"drop:{reason}", 0) + 1
        _escalate(
            escalations,
            {
                "kind": "dropped",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "reason": drop.get("reason"),
                "location": drop.get("location"),
                "detail": str(drop.get("detail"))[:200],
            },
        )


def _write_proposals(
    ctx: DispatchContext,
    *,
    proposals: list[RunProposal],
    chunk: dict[str, Any],
    stats: dict[str, int],
    escalations: list[dict[str, Any]],
    glyph_flagged: bool,
    model_used: str | None,
    raw_results: Any,
) -> None:
    """The ``insert_measure`` write loop both extraction paths share —
    per-proposal dup-skip, glyph-unit repair, write-refusal and flag
    accounting. ``raw_results`` is the reader reply's ``results`` list
    (``None`` for a recipe run, whose locations are ``table cell r:c``
    and whose raw fields already live on the spec)."""
    for proposal in proposals:
        stats["proposals"] += 1
        # gr503034: an emitted value_form that isn't a registered form is
        # dropped so the literal's shape infers one — count it so the
        # trial sees how often castor invents a form.
        raw_form = _result_field(raw_results, proposal.location, "value_form")
        if raw_form is not None and proposal.output.value_form is None:
            stats["value_form_inferred"] += 1
            _escalate(
                escalations,
                {
                    "kind": "value_form_inferred",
                    "chunk": chunk["chunk_id"],
                    "chunk_kind": chunk["chunk_kind"],
                    "location": proposal.location,
                    "detail": (
                        f"emitted value_form {raw_form!r} discarded — "
                        "inferred from the literal"
                    ),
                },
            )
        try:
            run = ctx.store.insert_measure(
                proposal.output,
                proposal.inputs,
                actor=_ACTOR,
                model=model_used,
            )
        except BadInput as exc:
            if "duplicate of measure" in str(exc):
                stats["skipped_dup"] += 1
                continue
            raw_unit = (
                _result_field(raw_results, proposal.location, "unit")
                or proposal.output.reported_unit
            )
            repaired = (
                _glyph_repair_unit(raw_unit)
                if glyph_flagged and "unit" in str(exc)
                else None
            )
            if repaired is not None:
                try:
                    repaired_run = ctx.store.insert_measure(
                        replace(proposal.output, reported_unit=repaired),
                        proposal.inputs,
                        actor=_ACTOR,
                        model=model_used,
                    )
                except BadInput:
                    repaired_run = None
                if repaired_run is not None:
                    stats["written"] += 1
                    stats["flag:glyph_unit_repaired"] = (
                        stats.get("flag:glyph_unit_repaired", 0) + 1
                    )
                    _escalate(
                        escalations,
                        {
                            "kind": "flag",
                            "chunk": chunk["chunk_id"],
                            "chunk_kind": chunk["chunk_kind"],
                            "flag": "glyph_unit_repaired",
                            "location": proposal.location,
                            "measure": repaired_run.output_id,
                            "detail": f"unit repaired {raw_unit!r} -> {repaired!r}",
                        },
                    )
                    continue
            stats["dropped"] += 1
            stats["write_refused"] += 1
            # The raw literal and unit ride along so a refusal a
            # reviewer can repair by hand (a glyph-damaged unit like
            # "W mK^2" that no normalisation recovers, gr511458)
            # keeps the number it was refused on.
            entry = {
                "kind": "write_refused",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "location": proposal.location,
                "detail": str(exc)[:200],
                "literal": _result_field(raw_results, proposal.location, "literal")
                or proposal.output.literal,
                "unit": raw_unit,
            }
            if glyph_flagged:
                entry["glyph_suspected"] = True
            _escalate(escalations, entry)
            continue
        stats["written"] += 1
        for flag in proposal.flags:
            stats[f"flag:{flag}"] = stats.get(f"flag:{flag}", 0) + 1
            _escalate(
                escalations,
                {
                    "kind": "flag",
                    "chunk": chunk["chunk_id"],
                    "chunk_kind": chunk["chunk_kind"],
                    "flag": flag,
                    "location": proposal.location,
                    "measure": run.output_id,
                },
            )


def _process_chunk(
    ctx: DispatchContext,
    *,
    chunk: dict[str, Any],
    paper: int,
    paper_title: str | None,
    glyph_flagged: bool,
    vocab: dict[int, VocabEntry],
    run_token: str,
    tier: Tier,
    model_pin: str | None,
    stats: dict[str, int],
    escalations: list[dict[str, Any]],
    caption: tuple[int, str] | None = None,
    table_no: int | None = None,
    run_header: list[str] | None = None,
    reader_measures: bool = False,
    recipe_measures: bool = False,
) -> tuple[str | None, str | None, Any]:
    """One chunk end to end — ``(outcome, landed_placement, result)``.
    Outcome is ``'paused'`` when the router paused (the caller yields for
    the window rather than burning through it), ``'failed'`` when the
    consecutive-error cap says the lane is down, ``None`` otherwise. The
    landed placement is ``result.placement`` (``'local'``/``'cloud'``)
    when the routed call reports one — the caller's pacing signal;
    ``None`` when the call never ran or stamped none. ``result`` is the
    routed reply (``None`` when ``route()`` raised) so the caller can
    read ``retry_at`` / ``reason_class`` for its park annotation.

    A ``table`` chunk whose grid parses and isn't ragged takes the recipe
    path first: one ``survey_extract:table_recipe`` call maps the grid to
    measurands, ``expand_recipe`` turns it into runs, and the same write
    loop lands them. A grid that doesn't parse, a ragged table, or a
    truncated/unparseable recipe reply keeps the per-cell reader path,
    so nothing regresses."""

    def reader_req() -> LlmRequest:
        return LlmRequest(
            tier=tier,
            prompt=_prompt.reader_prompt(
                vocab=sorted(vocab.values(), key=lambda e: e.slug),
                chunk_text=chunk["text"],
                section=chunk.get("section"),
                paper_title=paper_title,
            ),
            source="survey_extract",
            ref_id=paper,
            model=model_pin,
            placement=None if model_pin else "local",
            thinking=False,
            temperature=0.0,
            # Unpinned on purpose: a caller cap beats the chain rung's
            # max_tokens, and the prod llm.op.survey_extract row carries 4096
            # (gpt-oss reasoning tokens eat a small cap and truncation then
            # reads as unparseable JSON — the mismeasurement this split makes
            # visible).
            max_tokens=None,
            log_blobs=False,
        )

    if chunk["chunk_kind"] == "table":
        parsed = parse_table(chunk["chunk_id"], chunk["text"], header=run_header)
        if parsed is None:
            stats["table:fallback"] += 1
        elif parsed.ragged_rows:
            stats["table:ragged"] += 1
            _record_drops(
                [
                    {
                        "location": "table",
                        "reason": "ragged_table",
                        "detail": (
                            f"rows {parsed.ragged_rows} have a cell count "
                            f"other than {len(parsed.columns)} — column "
                            "alignment is unprovable"
                        ),
                    }
                ],
                chunk=chunk,
                stats=stats,
                escalations=escalations,
            )
        elif reader_measures:
            # Opus ruling 6: this run's chunks already hold reader-path
            # rows — the recipe would write a second copy (its cell
            # spans don't dedupe against reader spans), so the recipe
            # call is skipped and the chunk keeps the reader path.
            stats["table:reader_skip"] += 1
            _escalate(
                escalations,
                {
                    "kind": "flag",
                    "chunk": chunk["chunk_id"],
                    "chunk_kind": chunk["chunk_kind"],
                    "flag": "recipe_skip",
                    "detail": "skipped: reader measures exist",
                },
            )
        else:
            outcome, landed, result = _routed(
                LlmRequest(
                    tier=tier,
                    prompt=_prompt.table_recipe_prompt(
                        parsed=parsed,
                        caption=caption,
                        vocab=sorted(vocab.values(), key=lambda e: e.slug),
                        paper_title=paper_title,
                    ),
                    source="survey_extract:table_recipe",
                    ref_id=paper,
                    # The op is registered — route() resolves its model
                    # (a call-site pin would be ignored anyway).
                    model=None,
                    placement=None if model_pin else "local",
                    thinking=False,
                    temperature=0.0,
                    # Unpinned for the reader's reason — a caller cap
                    # under the op row's 4096 truncates a many-series
                    # recipe behind gpt-oss reasoning tokens and reads
                    # as unparseable (Opus finding 4).
                    max_tokens=None,
                    log_blobs=False,
                ),
                chunk=chunk,
                stats=stats,
                escalations=escalations,
            )
            if outcome is not None:
                return outcome, landed, result
            reply = _prompt.parse_reply(result.text)
            model_used = getattr(result, "model", None) or model_pin
            if reply is None or "table" not in reply:
                # A truncated or unparseable recipe — including a reply
                # that isn't a recipe at all (no "table" key) — falls
                # back to the per-cell reader for this chunk.
                stats["table:fallback"] += 1
                stats["dropped"] += 1
                stop = getattr(result, "stop_reason", None)
                truncated = stop in _TRUNCATION_STOPS
                stats["truncated" if truncated else "unparseable_reply"] += 1
                _escalate(
                    escalations,
                    {
                        "kind": "truncated" if truncated else "unparseable_reply",
                        "chunk": chunk["chunk_id"],
                        "chunk_kind": chunk["chunk_kind"],
                        "stop_reason": stop,
                        "detail": (result.text or "")[:200],
                    },
                )
            else:
                stats[
                    "table:no_results" if reply.get("table") is None else "table:recipe"
                ] += 1
                runs, drops = _recipes.expand_recipe(
                    parsed,
                    reply,
                    chunk_id=chunk["chunk_id"],
                    caption=caption,
                    vocab=vocab,
                    paper=paper,
                    table=table_no,
                    chunk_text=chunk["text"],
                    extraction_run=run_token,
                    model=model_used,
                )
                _record_drops(drops, chunk=chunk, stats=stats, escalations=escalations)
                _write_proposals(
                    ctx,
                    proposals=runs,
                    chunk=chunk,
                    stats=stats,
                    escalations=escalations,
                    glyph_flagged=glyph_flagged,
                    model_used=model_used,
                    raw_results=None,
                )
                return None, landed, result

    if recipe_measures:
        # Ruling 6 is symmetric (Opus r2 B): the table already holds
        # recipe-path rows — the reader's sentence anchors don't
        # dedupe against the recipe's cell spans, so a reader fallback
        # (unparseable/truncated recipe reply, unparseable or ragged
        # grid) would write a second copy. The reader call is skipped;
        # the recipe rows stand.
        stats["table:recipe_kept"] += 1
        _escalate(
            escalations,
            {
                "kind": "flag",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "flag": "reader_skip",
                "detail": "skipped: recipe measures exist",
            },
        )
        return None, None, None

    outcome, landed, result = _routed(
        reader_req(), chunk=chunk, stats=stats, escalations=escalations
    )
    if outcome is not None:
        return outcome, landed, result
    reply = _prompt.parse_reply(result.text)
    model_used = getattr(result, "model", None) or model_pin
    if reply is None:
        stats["dropped"] += 1
        stop = getattr(result, "stop_reason", None)
        truncated = stop in _TRUNCATION_STOPS
        stats["truncated" if truncated else "unparseable_reply"] += 1
        _escalate(
            escalations,
            {
                "kind": "truncated" if truncated else "unparseable_reply",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "stop_reason": stop,
                "detail": (result.text or "")[:200],
            },
        )
        return None, landed, result
    outcome_runs = build_runs(
        reply,
        chunk_id=chunk["chunk_id"],
        chunk_text=chunk["text"],
        paper_ref_id=paper,
        vocab=vocab,
        extraction_run=run_token,
        model=model_used,
    )
    _record_drops(
        outcome_runs.dropped, chunk=chunk, stats=stats, escalations=escalations
    )
    for oflag in outcome_runs.flags:
        flag_name = str(oflag.get("flag") or "unknown")
        stats[f"flag:{flag_name}"] = stats.get(f"flag:{flag_name}", 0) + 1
        _escalate(
            escalations,
            {
                "kind": "flag",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "flag": oflag.get("flag"),
                "location": oflag.get("location"),
                "detail": str(oflag.get("detail"))[:200],
            },
        )
    raw_results = reply.get("results") or []
    if not outcome_runs.proposals and not outcome_runs.dropped:
        # A parsed reply that produced nothing — no proposals, no named
        # drops — leaves no trace in the counters otherwise (gr512371:
        # five approved b1 rows vanished behind a silent results=[]).
        stats["empty_result"] += 1
        _escalate(
            escalations,
            {
                "kind": "empty_result",
                "chunk": chunk["chunk_id"],
                "chunk_kind": chunk["chunk_kind"],
                "detail": f"reader reply yielded nothing ({len(raw_results)} results)",
            },
        )
    _write_proposals(
        ctx,
        proposals=outcome_runs.proposals,
        chunk=chunk,
        stats=stats,
        escalations=escalations,
        glyph_flagged=glyph_flagged,
        model_used=model_used,
        raw_results=raw_results,
    )
    return None, landed, result


def _survey_dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Any:
    state = (ctx.meta or {}).get("coordinator_state") or {}
    phase = state.get("phase") or "extract"
    if phase == "extract":
        return _phase_extract(ctx, state)
    if phase == "await_accept":
        return _phase_await(ctx, state)
    return Done(summary=f"survey_extract: unknown phase {phase!r}", success=False)


def _phase_extract(ctx: DispatchContext, state: dict[str, Any]) -> Any:
    params = (ctx.meta or {}).get("params") or {}
    paper = _paper_id(ctx.store, params.get("paper"))
    if paper is None:
        return Done(
            summary="survey_extract: params.paper does not resolve to a live ref",
            success=False,
        )
    chunks = _chunks(ctx.store, paper)
    run_token = str(
        params.get("run_token")
        or state.get("run_token")
        or f"survey-extract:{ctx.ref_id}"
    )
    stats = {**_fresh_stats(), **dict(state.get("stats") or {})}
    escalations = list(state.get("escalations") or [])
    idx = int(state.get("idx") or 0)
    per_slice = _chunks_per_slice(params)
    min_chars = _min_chars(params)
    include_all = bool(params.get("include_all_sections"))
    tier = tier_from_str(params.get("tier") or "big", default=Tier.BIG)
    model_pin = params.get("model") if isinstance(params.get("model"), str) else None
    refusal = placement_refusal(
        LlmRequest(
            tier=tier,
            source="survey_extract",
            ref_id=paper,
            model=model_pin,
            placement=None if model_pin else "local",
        )
    )
    if refusal is not None:
        return Done(
            summary=(
                f"survey_extract: {refusal} — steer it with an "
                "llm.op.survey_extract row (`precis llm op set "
                "survey_extract --local <model>`) or pin params.model"
            ),
            success=False,
        )
    title = _paper_title(ctx.store, paper)
    glyph_flagged = _paper_glyph_flag(ctx.store, paper)
    allow = _measurand_allow(ctx.store, params.get("measurands"))
    kinds = _chunk_kinds_sel(ctx.store, params.get("chunk_kinds"))
    vocab = _vocab(
        ctx.store,
        (None if allow else _measurand_root(ctx.store, params.get("measurand_root"))),
        allow,
    )

    # The recipe path's caption/table-number lookups, computed once over
    # the loaded chunks — slice 1's assign_captions pairing (one-to-one,
    # number-checked) and table_runs' logical-table ordinals (the T<n>
    # meta.table.table carries).
    table_seq = [
        (c["chunk_id"], c["ord"], table_sig(c["text"]))
        for c in chunks
        if c["chunk_kind"] == "table"
    ]
    captions = assign_captions(
        table_seq,
        [
            (c["chunk_id"], c["ord"], c["text"])
            for c in chunks
            if c["chunk_kind"] != "table"
        ],
    )
    runs = table_runs(table_seq)
    table_no = {cid: g_idx + 1 for g_idx, run in enumerate(runs) for cid, _ord in run}
    headers = run_headers({c["chunk_id"]: c["text"] for c in chunks}, table_seq, runs)
    # Opus ruling 6: a table the reader path already extracted keeps
    # its rows — the recipe call skips it rather than writing a
    # second copy at spans the reader's anchors don't dedupe against.
    reader_tables = {
        table_no[cid]
        for cid in ctx.store.non_recipe_measure_chunks(
            [cid for cid, _o, _s in table_seq],
            prompt=_recipes.RECIPE_PROMPT_VERSION,
        )
    }
    # The ruling is symmetric (Opus r2 B): a table holding recipe-path
    # rows must not fall back to the reader either — same spans mismatch.
    recipe_tables = {
        table_no[cid]
        for cid in ctx.store.recipe_measure_chunks(
            [cid for cid, _o, _s in table_seq],
            prompt=_recipes.RECIPE_PROMPT_VERSION,
        )
    }

    if ctx.is_cancel_requested():
        return Done(summary="survey_extract cancelled", success=False)

    processed = 0
    # ``None`` = no routed call has reported a placement yet; the paced
    # loop below treats it like remote. A ``local`` landing flips the
    # pacing off — see the module docstring.
    local_lane: bool | None = None
    pause_streak = int(state.get("pause_streak") or 0)
    while idx < len(chunks) and (processed < per_slice or local_lane):
        chunk = chunks[idx]
        idx += 1
        if not include_all and _skippable(chunk, kinds):
            stats["skipped_section"] += 1
            continue
        if len(chunk["text"].strip()) < min_chars:
            stats["skipped_short"] += 1
            continue
        processed += 1
        stats["chunks"] += 1
        outcome, landed, routed = _process_chunk(
            ctx,
            chunk=chunk,
            paper=paper,
            paper_title=title,
            glyph_flagged=glyph_flagged,
            vocab=vocab,
            run_token=run_token,
            tier=tier,
            model_pin=model_pin,
            stats=stats,
            escalations=escalations,
            caption=captions.get(chunk["chunk_id"]),
            table_no=table_no.get(chunk["chunk_id"]),
            run_header=headers.get(chunk["chunk_id"]),
            reader_measures=(table_no.get(chunk["chunk_id"]) in reader_tables),
            recipe_measures=(table_no.get(chunk["chunk_id"]) in recipe_tables),
        )
        if landed is not None:
            local_lane = landed == "local"
        if outcome == "failed":
            return Done(
                summary=(
                    f"survey_extract: the reader failed "
                    f"{stats.get('consecutive_errors')} chunks running — "
                    "the local lane looks down"
                ),
                success=False,
                summary_meta={"stats": stats},
            )
        if outcome == "paused":
            pause_streak += 1
            if pause_streak >= _max_pauses():
                return Done(
                    summary=(
                        f"survey_extract: the reader lane stayed saturated — "
                        f"{pause_streak} consecutive paused slices (cap "
                        f"{_max_pauses()}); the local slot never freed, "
                        "giving up rather than parking forever"
                    ),
                    success=False,
                    summary_meta={"stats": stats},
                )
            reason_class = getattr(routed, "reason_class", None)
            why = (
                "paused: local reader lane saturated"
                + (f" ({reason_class})" if reason_class else "")
                + f" · pause streak {pause_streak}/{_max_pauses()}"
            )
            return Yield(
                state={
                    "phase": "extract",
                    "idx": idx - 1,
                    "stats": stats,
                    "escalations": escalations,
                    "run_token": run_token,
                    "pause_streak": pause_streak,
                },
                wake_when=_retry_wake(routed, why=why),
            )
        # The lane answered — a pause sandwiched between answered calls is
        # a blip, not a wedge; only an unbroken streak counts.
        pause_streak = 0
        # The unrolled local loop still answers a cancel at the old
        # per-slice cadence — a 600-chunk run must not become
        # uncancellable for its ~30 min.
        if local_lane and processed % per_slice == 0 and ctx.is_cancel_requested():
            return Done(summary="survey_extract cancelled", success=False)

    if idx < len(chunks):
        if ctx.is_cancel_requested():
            return Done(summary="survey_extract cancelled", success=False)
        return Yield(
            state={
                "phase": "extract",
                "idx": idx,
                "stats": stats,
                "escalations": escalations,
                "run_token": run_token,
                "pause_streak": pause_streak,
            },
            wake_when=_wake_s(
                1, why=f"pacing: {idx}/{len(chunks)} chunks read — next slice"
            ),
        )

    # Extraction done — spawn the acceptance child (unless skipped).
    if params.get("skip_accept"):
        acc_note = "acceptance skipped (params.skip_accept)"
        accept_child = None
    else:
        accept_child = ctx.spawn_child(
            "measure_accept",
            executor="coordinator",
            params={
                "paper": paper,
                "extraction_run": run_token,
                "sample_n": params.get("sample_n"),
                "bar": params.get("bar"),
                "tier": params.get("accept_tier"),
            },
            model=params.get("accept_model"),
            idem_key=f"measure-accept:{ctx.ref_id}",
        )
        acc_note = f"acceptance delegated to measure_accept job {accept_child}"

    ctx.append_chunk(
        "job_event",
        "extract done: "
        + ", ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        + f" · escalations={len(escalations)}",
    )
    if accept_child is not None:
        return Yield(
            state={
                "phase": "await_accept",
                "stats": stats,
                "escalations": escalations,
                "run_token": run_token,
                "accept_child": accept_child,
            },
            wake_when=WakeWhen("children_done", {"child_job_ids": [accept_child]}),
        )
    return _survey_done(ctx, stats, escalations, run_token, acc_note, None)


def _phase_await(ctx: DispatchContext, state: dict[str, Any]) -> Any:
    child = state.get("accept_child")
    if not isinstance(child, int):
        return _survey_done(
            ctx,
            dict(state.get("stats") or {}),
            list(state.get("escalations") or []),
            str(state.get("run_token") or ""),
            "no accept child recorded",
            None,
        )
    with ctx.store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.meta, COALESCE("
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id"
            "   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),"
            "  'queued') "
            "FROM refs r WHERE r.ref_id = %s",
            (child,),
        ).fetchone()
    status = "missing" if row is None else str(row[1])
    if status in {"queued", "running", "claimed"} or status.startswith("waiting"):
        if ctx.is_cancel_requested():
            return Done(summary="survey_extract cancelled", success=False)
        return Yield(
            state=state,
            wake_when=_wake_s(
                120, why=f"awaiting accept child jo{child} (STATUS:{status})"
            ),
        )
    acceptance = None
    if row is not None and isinstance(row[0], dict):
        acceptance = row[0].get("acceptance")
    note = f"accept child {child} ended {status}"
    return _survey_done(
        ctx,
        dict(state.get("stats") or {}),
        list(state.get("escalations") or []),
        str(state.get("run_token") or ""),
        note,
        acceptance if isinstance(acceptance, dict) else None,
    )


def _zero_write_failure(
    stats: dict[str, Any], escalations: list[dict[str, Any]]
) -> str | None:
    """Failure line when chunks were attempted, errors occurred and nothing
    was written; ``None`` for a legitimately empty run."""
    if int(stats.get("written") or 0) > 0 or int(stats.get("llm_errors") or 0) <= 0:
        return None
    counts: dict[str, int] = {}
    for e in escalations:
        if e.get("kind") in {"llm_exception", "llm_error"}:
            d = str(e.get("detail") or "")
            counts[d] = counts.get(d, 0) + 1
    dominant = max(counts, key=lambda d: counts[d]) if counts else "unknown error"
    return (
        f"FAILED: 0 rows written, {stats.get('llm_errors')} of "
        f"{stats.get('calls')} chunk calls errored; dominant error: {dominant}"
    )


def _survey_done(
    ctx: DispatchContext,
    stats: dict[str, Any],
    escalations: list[dict[str, Any]],
    run_token: str,
    accept_note: str,
    acceptance: dict[str, Any] | None,
) -> Done:
    head = f"survey_extract done ({run_token}): " + ", ".join(
        f"{k}={v}" for k, v in sorted(stats.items())
    )
    lines = [head, f"acceptance: {accept_note}"]
    if acceptance:
        lines.append(
            "batch "
            + ("ACCEPTED" if acceptance.get("accepted") else "FAILED")
            + f" — {acceptance.get('errors')} errors in "
            + f"{acceptance.get('sampled')} sampled "
            + f"(Wilson upper {acceptance.get('wilson_hi')}"
            f" vs bar {acceptance.get('bar')})"
            + (
                f"; {acceptance['unanchored']} unanchored, excluded"
                if acceptance.get("unanchored")
                else ""
            )
        )
    if escalations:
        lines.append(f"escalations ({len(escalations)}):")
        for e in escalations[:50]:
            lines.append("  " + ", ".join(f"{k}={v}" for k, v in e.items()))
    failure = _zero_write_failure(stats, escalations)
    if failure:
        lines.insert(1, failure)
    ctx.append_chunk("job_summary", "\n".join(lines))
    meta: dict[str, Any] = {
        "extraction_run": run_token,
        "stats": stats,
        "drops_by_reason": {
            k[len("drop:") :]: v
            for k, v in sorted(stats.items())
            if k.startswith("drop:") and v
        },
        "flags_by_kind": {
            k[len("flag:") :]: v
            for k, v in sorted(stats.items())
            if k.startswith("flag:") and v
        },
        "tables": {
            name: stats.get(f"table:{name}", 0)
            for name in ("recipe", "fallback", "no_results", "ragged")
        },
        "escalation_count": len(escalations),
        "escalations": escalations,
    }
    if acceptance:
        meta["acceptance"] = acceptance
    return Done(summary="\n".join(lines), success=failure is None, summary_meta=meta)


def validate(store: Store, params: dict[str, Any]) -> None:
    """``JobTypeSpec.validate`` (gr511724) — the put-time half of the
    dispatch's store-backed refusals: ``paper`` must resolve to a live
    ref and every ``measurands`` entry must be a live taxon. The
    dispatch runs the same checks (``_paper_id`` / ``_measurand_allow``
    — the latter is where the BadInput already lives); running them at
    put refuses the submit instead of leaving a failed job."""
    if _paper_id(store, params.get("paper")) is None:
        raise BadInput(
            "survey_extract: params.paper does not resolve to a live ref",
            next="pass a live paper ref_id, pa<id> handle or slug",
        )
    _measurand_allow(store, params.get("measurands"))
    _chunk_kinds_sel(store, params.get("chunk_kinds"))


SPEC = JobTypeSpec(
    name="survey_extract",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate=validate,
    dispatch=_survey_dispatch,
)


# ── measure_accept ──────────────────────────────────────────────────

ACCEPT_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "paper": {"type": ["integer", "string"]},
        "extraction_run": {"type": ["string", "null"]},
        "sample_n": {"type": ["integer", "null"]},
        "bar": {"type": ["number", "null"]},
        "z": {"type": ["number", "null"]},
        "tier": {"type": ["string", "null"]},
        "model": {"type": ["string", "null"]},
        "reviews_per_slice": {"type": ["integer", "null"]},
    },
    "required": [],
    "additionalProperties": True,
}

ACCEPT_DESCRIPTION = (
    "Sampling acceptance of one extraction batch — stratified sample of "
    "the batch's output rows (paper × measurand), frontier-model verdicts "
    "on the reviews ledger, Wilson interval verdict on the batch."
)


def _reviews_per_slice(params: dict[str, Any]) -> int:
    raw = params.get("reviews_per_slice")
    if isinstance(raw, int) and not isinstance(raw, bool) and 1 <= raw <= 50:
        return raw
    return env_int("PRECIS_MEASURE_ACCEPT_PER_SLICE", 6, lo=1, hi=50)


def _batch_rows(
    store: Store, paper: int | None, run_token: str
) -> list[dict[str, Any]]:
    """The batch's live output rows — the run token's rows on ``paper``, or
    across papers when ``paper`` is None (the batch *is* the run token —
    corpus finding 1). Newest-first is irrelevant: the sample is drawn
    deterministically over the id set. A row with no anchor
    (``primary_link_id`` NULL — legal for ``tier='asserted'``, and the
    state an anchored row ends in when its link is deleted) is still a
    batch row: it lands flagged ``anchored=False`` and reads as
    ``unanchored`` when sampled — named in the output, counted apart,
    out of the Wilson math (gr515505)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT m.id, m.measurand_ref_id, m.literal, m.reported_unit, "
            "       m.subject, m.span, m.anchor_scheme, m.run_key, "
            "       m.conditions, l.src_chunk_id, t.meta->>'slug', "
            "       m.subject_ref_id, m.meta->'table', m.value_num, "
            "       m.meta->>'transform' "
            "  FROM measures m "
            "  LEFT JOIN links l ON l.link_id = m.primary_link_id "
            "  LEFT JOIN refs t ON t.ref_id = m.measurand_ref_id "
            " WHERE (%s::bigint IS NULL OR m.subject_ref_id = %s) "
            "   AND m.direction = 'output' "
            "   AND m.superseded_by IS NULL "
            "   AND m.meta->>'extraction_run' = %s ORDER BY m.id",
            (paper, paper, run_token),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "measurand_ref_id": r[1],
            "literal": r[2],
            "reported_unit": r[3],
            "subject": r[4],
            "span": r[5],
            "anchor_scheme": r[6],
            "run_key": r[7],
            "conditions": r[8],
            "chunk_id": r[9],
            "anchored": r[9] is not None,
            "measurand": r[10] or str(r[1]),
            "subject_ref_id": int(r[11]),
            "table_meta": r[12] if isinstance(r[12], dict) else None,
            "value_num": r[13],
            "transform": r[14],
        }
        for r in rows
    ]


def _latest_run_token(store: Store, paper: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'extraction_run' AS tok, max(created_at) "
            "  FROM measures WHERE subject_ref_id = %s "
            "   AND meta ? 'extraction_run' GROUP BY tok "
            " ORDER BY max(created_at) DESC LIMIT 1",
            (paper,),
        ).fetchone()
    return str(row[0]) if row and row[0] else None


def _chunk_text(store: Store, chunk_id: Any) -> str | None:
    with store.pool.connection() as conn:
        r = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    return str(r[0]) if r and r[0] else None


#: The review prompt's evidence cap for a table-row span: a wide grid's
#: ``row_span`` slice can run to tens of KB and flood the judge, so a
#: longer row is trimmed to a window centered on the reviewed cell's
#: offsets — the sibling cells nearest it stay in view.
_SPAN_TEXT_CAP = 2000


def _span_text(
    store: Store, row: dict[str, Any], table_ctx: dict[str, Any] | None = None
) -> str | None:
    """The row's evidence text: for a cell-anchored table row the whole
    table ROW's slice (``meta.table.row_span``) — a bare ``'0.37'`` cell
    alone is unverifiable; the row prints the subject label and sibling
    cells (gr506762 slice-4: ~55 of 60 sampled rows read 'undecidable'
    on a bare-number span). Otherwise the span's slice for an offsets
    anchor, the whole chunk otherwise (mirrors the store's own
    _span_text)."""
    with store.pool.connection() as conn:
        r = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (row["chunk_id"],)
        ).fetchone()
    if r is None:
        return None
    text = str(r[0] or "")
    row_span = (table_ctx or {}).get("row_span")
    if (
        isinstance(row_span, list)
        and len(row_span) == 2
        and all(isinstance(x, int) and not isinstance(x, bool) for x in row_span)
    ):
        start, end = row_span
        if end - start > _SPAN_TEXT_CAP:
            span = row.get("span")
            mid = (
                (span[1] + span[2]) // 2
                if isinstance(span, list)
                and len(span) == 3
                and all(isinstance(x, int) for x in span)
                else (start + end) // 2
            )
            start = max(start, min(mid - _SPAN_TEXT_CAP // 2, end - _SPAN_TEXT_CAP))
            end = start + _SPAN_TEXT_CAP
        return text[start:end]
    span = row.get("span")
    if (
        isinstance(span, list)
        and len(span) == 3
        and all(isinstance(x, int) for x in span)
    ):
        return text[span[1] : span[2]]
    return text


def _row_conditions(
    store: Store, run_key: str, output_conditions: Any = None
) -> list[str]:
    """The run's conditions for the sampler: the output row's named
    context dict (baseline, period, model — gr511461) plus its input
    rows."""
    out = [
        f"{k} = {v}" for k, v in (output_conditions or {}).items() if isinstance(v, str)
    ]
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'condition', literal FROM measures "
            "WHERE run_key = %s AND direction = 'input' ORDER BY id",
            (run_key,),
        ).fetchall()
    out += [f"{cond or '?'} = {lit}" for cond, lit in rows if isinstance(lit, str)]
    return out


_VERDICT_TO_REVIEW = {
    "correct": "approved",
    "wrong": "rejected",
    "undecidable": "proposed",
}


def _accept_dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> Any:
    state = (ctx.meta or {}).get("coordinator_state") or {}
    params = (ctx.meta or {}).get("params") or {}
    raw_paper = params.get("paper")
    paper = _paper_id(ctx.store, raw_paper)
    if raw_paper is not None and paper is None:
        return Done(
            summary="measure_accept: params.paper does not resolve to a live ref",
            success=False,
        )
    raw_token = params.get("extraction_run")
    token = raw_token.strip() if isinstance(raw_token, str) else ""
    if paper is None and not token:
        return Done(
            summary=(
                "measure_accept: needs params.extraction_run (the batch is "
                "the run token) or a resolvable params.paper"
            ),
            success=False,
        )
    run_token = token or str(
        _latest_run_token(ctx.store, paper) if paper is not None else ""
    )
    if not run_token:
        return Done(
            summary=(
                f"measure_accept: no extraction_run given and none found on "
                f"paper {paper}"
            ),
            success=False,
        )
    rows = _batch_rows(ctx.store, paper, run_token)
    if not rows:
        scope = f"on paper {paper}" if paper is not None else "on any paper"
        return Done(
            summary=(
                f"measure_accept: no live output rows for run {run_token!r} {scope}"
            ),
            success=False,
        )

    if "sampled" not in state:
        # The put-time refusal is hours stale by dispatch — re-check that
        # no extract job still carries the token before reading the frame.
        if paper is None and _token_extract_live(ctx.store, run_token):
            return Done(
                summary=(
                    f"measure_accept: survey_extract jobs for run "
                    f"{run_token!r} are still live — re-run once they finish"
                ),
                success=False,
            )
        # Strata are paper × measurand (the 2026-10-14 ruling); for a
        # paper-scoped batch the paper factor is constant, so this is the
        # same draw the per-measurand strata gave before.
        strata: dict[str, list[int]] = {}
        for r in rows:
            strata.setdefault(f"{r['subject_ref_id']}:{r['measurand']}", []).append(
                r["id"]
            )
        draw = _accept.stratified_sample(
            strata,
            _sample_n(params),
            seed=run_token,
        )
        state = {
            **state,
            "sampled": [mid for ids in draw.values() for mid in ids],
            "verdicts": {},
            "idx": 0,
            "total_rows": len(rows),
            "run_token": run_token,
            # The sampling frame, frozen at the draw: rows and papers
            # committed during the review slices (a budget pause can
            # last hours) were never sampleable and must not land
            # inside coverage.
            "frame_papers": sorted({r["subject_ref_id"] for r in rows}),
            "frame_max_id": max(r["id"] for r in rows),
        }
    sampled = [int(m) for m in state.get("sampled") or []]
    verdicts = dict(state.get("verdicts") or {})
    idx = int(state.get("idx") or 0)
    by_id = {r["id"]: r for r in rows}
    per_slice = _reviews_per_slice(params)
    tier = tier_from_str(params.get("tier") or "frontier", default=Tier.FRONTIER)
    model_pin = params.get("model") if isinstance(params.get("model"), str) else None

    processed = 0
    while idx < len(sampled) and processed < per_slice:
        if ctx.is_cancel_requested():
            return Done(summary="measure_accept cancelled", success=False)
        mid = sampled[idx]
        idx += 1
        measure = by_id.get(mid)
        if measure is None:
            verdicts[str(mid)] = {"verdict": "undecidable", "reason": "row gone"}
            continue
        if not measure.get("anchored"):
            # In the batch and on the output, out of the verdict math:
            # there is no passage to check the literal against, so the
            # row is unverifiable — but unverifiable is not a sampled
            # error either (gr515505's first cut counted it as one, which
            # flipped re-accepts of anchor-lost batches to FAILED; the
            # gripe asked for visibility, not new math). It is flagged
            # "unanchored", named in the summary, and counted apart —
            # see the "N unanchored, excluded" note below. Two distinct
            # reasons: never anchored (legal for tier='asserted') vs the
            # anchor was recorded and its link died afterwards (a
            # deleted chunk/paper NULLs primary_link_id, anchor_scheme
            # stays stamped).
            verdicts[str(mid)] = {
                "verdict": "unanchored",
                "reason": (
                    "anchor lost — the anchored link is gone, unverifiable"
                    if measure.get("anchor_scheme")
                    else "no anchor on the row — unverifiable"
                ),
            }
            continue
        table_ctx = measure.get("table_meta")
        if isinstance(table_ctx, dict):
            # The reviewer's table context: the series label, the cell's
            # 1-based coordinates and the paired caption's text —
            # without them the sample is one bare cell with no column
            # name.
            table_ctx = dict(table_ctx)
            cap_text = _chunk_text(ctx.store, table_ctx.get("caption_chunk"))
            if cap_text:
                table_ctx["caption"] = cap_text
        else:
            table_ctx = None
        span = _span_text(ctx.store, measure, table_ctx)
        if span is None:
            verdicts[str(mid)] = {
                "verdict": "undecidable",
                "reason": "anchor chunk missing",
            }
            continue
        result = route(
            LlmRequest(
                tier=tier,
                prompt=_prompt.review_prompt(
                    literal=str(measure["literal"] or ""),
                    reported_unit=measure["reported_unit"],
                    measurand=str(measure["measurand"]),
                    subject_label=measure["subject"],
                    span_text=span,
                    conditions=_row_conditions(
                        ctx.store,
                        str(measure["run_key"] or ""),
                        measure.get("conditions"),
                    ),
                    table=table_ctx,
                    transform=measure.get("transform"),
                    stored_value=measure.get("value_num"),
                ),
                source="measure_accept",
                ref_id=measure["subject_ref_id"],
                model=model_pin,
                thinking=False,
                temperature=0.0,
                max_tokens=256,
                log_blobs=False,
            )
        )
        if getattr(result, "paused", False):
            # Yield, not defer_on_pause: the Yield persists the verdicts
            # already earned this run, a park would lose the slice's work.
            return Yield(
                state={**state, "idx": idx - 1, "verdicts": verdicts},
                wake_when=_retry_wake(result),
            )
        if getattr(result, "error", None):
            verdicts[str(mid)] = {
                "verdict": "undecidable",
                "reason": f"llm_error: {str(result.error)[:120]}",
            }
            continue
        verdict, reason = _prompt.parse_verdict(result.text)
        model_used = getattr(result, "model", None) or model_pin or resolve_model(tier)
        try:
            ctx.store.record_target_review(
                "measure",
                mid,
                actor="measure-accept",
                verdict=_VERDICT_TO_REVIEW[verdict],
                model=model_used,
                version="1",
                note=reason[:500] if reason else None,
            )
        except Exception as exc:  # row vanished between fetch and review
            log.info("measure_accept: review of mx%s failed: %s", mid, exc)
            verdict = "undecidable"
            reason = f"review write failed: {exc}"
        verdicts[str(mid)] = {"verdict": verdict, "reason": reason}
        processed += 1

    if idx < len(sampled):
        return Yield(
            state={**state, "idx": idx, "verdicts": verdicts},
            wake_when=_wake_s(1),
        )

    # "unanchored" verdicts are visibility, not evidence: they sit out of
    # both Wilson terms (numerator AND denominator) so a re-accept of a
    # batch whose anchors died after extraction lands the same verdict
    # it did before they were drawn (gr515505 review).
    scored = [
        mid
        for mid in sampled
        if verdicts.get(str(mid), {}).get("verdict") != "unanchored"
    ]
    n_unanchored = len(sampled) - len(scored)
    errors = sum(1 for mid in scored if verdicts[str(mid)].get("verdict") != "correct")
    z = params.get("z") if isinstance(params.get("z"), (int, float)) else None
    bar = params.get("bar") if isinstance(params.get("bar"), (int, float)) else None
    acc = _accept.acceptance(
        errors,
        len(scored),
        z=float(z) if z else _accept.DEFAULT_Z,
        bar=float(bar) if bar else _accept.DEFAULT_BAR,
    )
    # Coverage is the sampling frame frozen at the draw (state above):
    # the papers in the frame plus the frame's row-id high-water mark —
    # rows minted under the token afterwards were never sampleable, and
    # accepted_runs grants by both. A checkpoint predating the frame
    # fields omits coverage entirely — re-reading the rows live now would
    # sweep in what was never sampleable, so the verdict carries no
    # coverage and accepted_runs reads it as non-counting (fail closed).
    papers = state.get("frame_papers")
    max_id = state.get("frame_max_id")
    covered = (
        isinstance(papers, list)
        and bool(papers)
        and isinstance(max_id, int)
        and not isinstance(max_id, bool)
    )
    acc_meta = {
        "accepted": acc.accepted,
        "errors": acc.errors,
        "sampled": acc.sampled,
        "error_rate": acc.error_rate,
        "wilson_lo": acc.wilson_lo,
        "wilson_hi": acc.wilson_hi,
        "z": acc.z,
        "bar": acc.bar,
        "run_token": run_token,
        "total_rows": int(state.get("total_rows") or len(rows)),
    }
    if n_unanchored:
        acc_meta["unanchored"] = n_unanchored
    if covered:
        acc_meta["papers"] = papers
        acc_meta["max_id"] = max_id
    ctx.set_meta(acceptance=acc_meta)
    verdict = "ACCEPTED" if acc.accepted else "FAILED"
    bad = [
        (mid, verdicts[str(mid)])
        for mid in scored
        if verdicts.get(str(mid), {}).get("verdict") != "correct"
    ]
    excluded = [
        (mid, verdicts[str(mid)])
        for mid in sampled
        if verdicts.get(str(mid), {}).get("verdict") == "unanchored"
    ]
    if paper is not None:
        scope = f"pa{paper}"
    elif covered and isinstance(papers, list):
        scope = f"{len(papers)} papers"
    else:
        scope = "the run"
    head = (
        f"measure_accept: batch {verdict} for run {run_token} on {scope}: "
        f"{acc.errors} error(s) in {acc.sampled} sampled "
        f"(Wilson upper {acc.wilson_hi:.3f} vs bar {acc.bar})"
    )
    if n_unanchored:
        head += f"; {n_unanchored} unanchored, excluded"
    lines = [head]
    for mid, v in bad[:50]:
        lines.append(f"  mx{mid}: {v.get('verdict')} — {v.get('reason') or ''}")
    for mid, v in excluded[:50]:
        lines.append(f"  mx{mid}: {v.get('verdict')} — {v.get('reason') or ''}")
    ctx.append_chunk("job_summary", "\n".join(lines))
    return Done(
        summary="\n".join(lines),
        success=True,
        summary_meta={"acceptance": acc_meta},
    )


def _sample_n(params: dict[str, Any]) -> int:
    raw = params.get("sample_n")
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return _accept.DEFAULT_SAMPLE


def _token_extract_live(store: Store, token: str) -> bool:
    """A live ``survey_extract`` job carrying ``token`` — unretired and
    without a terminal ``STATUS:`` tag (``_common.TERMINAL``; a job with
    no STATUS tag at all is not provably done, so it counts as live).
    The token rides the job's ``params.run_token``, its checkpointed
    ``coordinator_state.run_token``, or is the default
    ``survey-extract:<ref_id>`` the job derives on first dispatch."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM refs j "
            " WHERE j.kind = 'job' AND j.retired_at IS NULL "
            "   AND j.meta ->> 'job_type' = 'survey_extract' "
            "   AND NOT EXISTS ("
            "         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)"
            "          WHERE rt.ref_id = j.ref_id "
            "            AND t.namespace = 'STATUS' "
            "            AND t.value = ANY(%s)) "
            "   AND (j.meta -> 'params' ->> 'run_token' = %s "
            "        OR j.meta -> 'coordinator_state' ->> 'run_token' = %s "
            "        OR 'survey-extract:' || j.ref_id::text = %s) "
            " LIMIT 1",
            (list(TERMINAL), token, token, token),
        ).fetchone()
    return row is not None


def accept_validate(store: Store, params: dict[str, Any]) -> None:
    """``JobTypeSpec.validate`` for ``measure_accept`` — the batch is the
    run token, so ``extraction_run`` alone suffices; ``paper`` only narrows
    the batch to that paper's share and must resolve when given. A
    run-level accept is refused while extract jobs still carry the token:
    sequence order is not commit order, so a row can take an id under the
    frame's high-water mark yet commit after the draw."""
    raw_paper = params.get("paper")
    raw_token = params.get("extraction_run")
    has_token = isinstance(raw_token, str) and bool(raw_token.strip())
    if raw_paper is not None and _paper_id(store, raw_paper) is None:
        raise BadInput(
            "measure_accept: params.paper does not resolve to a live ref",
            next="pass a live paper ref_id, pa<id> handle or slug",
        )
    if _paper_id(store, raw_paper) is None and not has_token:
        raise BadInput(
            "measure_accept: needs params.extraction_run or params.paper",
            next="pass extraction_run='<run token>' (a paper alone means "
            "its latest extraction run)",
        )
    if raw_paper is None and has_token:
        token = str(raw_token).strip()
        if _token_extract_live(store, token):
            raise BadInput(
                f"measure_accept: survey_extract jobs for run {token!r} are still live",
                next="wait for the extraction jobs to finish — or pass "
                "paper=<id> for a paper whose extraction has finished",
            )


ACCEPT_SPEC = JobTypeSpec(
    name="measure_accept",
    params_schema=ACCEPT_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=ACCEPT_DESCRIPTION,
    validate=accept_validate,
    dispatch=_accept_dispatch,
)
