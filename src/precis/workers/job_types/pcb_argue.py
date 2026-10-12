"""``pcb_argue`` job_type — the LLM acts on a design's open arguments.

docs/backlog/pcb-argue-with-design.md slice 2, following gr335242 comment
1's playbook: an argument note is an instruction, not just a record — "so
the llm can fix problems". This job reads the open argument notes on ONE
pcb design (:func:`precis.utils.notes.open_questions` over
:meth:`precis.store._pcb_ops.PcbMixin.pcb_notes_list` is the work queue),
resolves each question's anchors through :mod:`precis.pcb.argue` — the
same grammar/resolver the board page's ``POST /pcb/{slug}/note`` uses,
typed and clicked handles alike — runs the real machinery once, and
answers each question in place: one ``answer`` note per question,
``re=`` its name, anchored to the same handles, ``origin='proposed'`` —
so the argument thread lives on the design.

The playbook, verbatim:

* **change request** → propose the edit (``origin='proposed'``, human
  accepts — the job changes nothing itself);
* **interference/geometry concern** → run the real machinery and answer
  with numbers. Here: one fresh placement DRC run
  (:func:`precis.pcb.session.placement_drc_findings` — pad/copper
  clearance, via keep-outs, courtyard overlap, outline containment, the
  same check the place/route jobs gate on), the LAST persisted full
  geometric DRC run (:meth:`Store.pcb_drc_findings_latest` — what
  ``view='drc'`` most recently measured over routed copper, named by its
  run_id so the answer can say how fresh it is), the authored measures'
  verdicts (:func:`precis.pcb.eyes.evaluate_measures`), and
  centre-to-centre distances between every pair of parts the argument
  names (:func:`precis.pcb.eyes.proximity`);
* **verification question** → answer from that evidence and say HONESTLY
  when the question exceeds it, naming the backlog item it waits on —
  never a confabulated yes (the ``act_prompt`` carries the instruction;
  a machinery run that could not answer the question is itself reported
  as evidence, so "not checked" is a sayable answer).

The selection catalogue the object handles (``via:x:y``, ``silk:...``,
``outline:N``, ``fiducial:N``) resolve against is built through
:func:`precis.pcb.selectables.build` over a model assembled the same way
the renders build theirs — :func:`precis.pcb.realize.pads_for_ir` pads +
:func:`precis.pcb.furniture.board_furniture` furniture, not a second
recipe. That discipline is why the furniture assembly lives in
``precis.pcb``: the workers->handlers layering contract (ship gate) keeps
this module off ``precis.handlers.pcb``.

Runs under ``job_inproc`` (bounded in-proc — ``params.limit`` caps the
open questions one run answers, the ``embed_batch`` self-limit
discipline). The model call goes through the router in-process
(:data:`precis.pcb.argue.LLM_SOURCE`, MEDIUM tier) — the same call the
board page's inline answer makes, never a ``claude -p`` subprocess, so
``claude_inproc``'s ``claude_bin`` requirement does not apply.
``put(kind='job', job_type='pcb_argue', parent_id=<pcb ref>)`` is the
mint door (pcb is a compute-lane parent kind); the web box answers
inline instead and never mints this job.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from itertools import combinations
from typing import TYPE_CHECKING, Any

from precis.pcb import argue
from precis.pcb import eyes as pcb_eyes
from precis.pcb import selectables as pcb_selectables
from precis.pcb import session as pcb_session
from precis.utils.notes import NoteSpec, open_questions
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pcb_ref_id": {"type": "integer"},
        # Bounded: the run answers at most `limit` open questions per
        # claim (one MEDIUM-tier call each) — job_inproc's self-limit
        # rule (embed_batch's params.limit is the precedent).
        "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        # Answer one named question only (retry a single argument).
        "question": {"type": "string", "minLength": 1},
    },
    "required": ["pcb_ref_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})
#: Nothing reserved: the model call is router-capped per call (MEDIUM
#: tier), the same reason derived_drain declares no executor slot.
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Answer a pcb design's open argument notes in place — resolve each "
    "question's handles, run the real machinery (fresh placement DRC + "
    "the last full run + measures + part-pair distances), store an "
    "'answer' note re each."
)

#: ``params.limit`` default — one run answers a handful of arguments,
#: never a backlog of hundreds in one claim.
_DEFAULT_LIMIT = 10

#: Per-question evidence cap on DRC lines — a flooded evidence block
#: dilutes the one finding the argument is about.
_MAX_DRC_LINES = 15

#: One evidence finding, normalized across its two sources:
#: ``{rule, severity, where, detail, objects, margin_mm, source}`` —
#: ``source`` is ``"placement-drc"`` (fresh run) or
#: ``"full-drc <run_id[:12]>"`` (the last persisted geometric run, which
#: saw routed copper the placement pass has not).
Finding = dict[str, Any]


def _board_inputs(
    store: Store, ref_id: int, design: dict[str, Any], graph: dict[str, Any]
) -> dict[str, Any] | None:
    """The shared board-side inputs the catalogue and the machinery both
    read — stackup/layers, outline, IR, footprint remap, copper. ``None``
    when the design has no board row yet (a fresh design before any
    route/generator pass): every consumer then degrades to the honest
    "nothing to check" note."""
    board = design.get("board") or {}
    board_id = board.get("board_id")
    if board_id is None:
        return None
    stackup = board.get("stackup") or []
    features = store.pcb_features_list(ref_id)
    outline = pcb_session.outline_from_features(features)
    fp_by_lcsc = store.pcb_footprints_for(ref_id)
    local_fps = store.pcb_local_footprints_for(ref_id)
    fixed_copper = store.pcb_fixed_copper_list(int(board_id))
    ir = pcb_session.build_ir(
        graph,
        outline=outline,
        mounting_holes=pcb_session.mounting_holes_from_features(features),
        footprints_by_lcsc=fp_by_lcsc,
        local_footprints_by_name=local_fps,
        fixed_copper=fixed_copper,
    )
    return {
        "board_id": int(board_id),
        "stackup": stackup,
        "layers": [str(layer.get("name")) for layer in stackup],
        "features": features,
        "outline": outline,
        "ir": ir,
        "footprints": pcb_session.footprints_by_refdes(
            ir,
            fp_by_lcsc,
            local_footprints_by_name=local_fps,
            local_names_by_refdes=pcb_session.local_footprint_names_by_refdes(graph),
        ),
        "fixed_copper": fixed_copper,
        "copper": store.pcb_copper_list(int(board_id)),
        "capability": pcb_session.stackup_capability(stackup),
    }


def _catalogue(
    store: Store,
    ref_id: int,
    slug: str,
    design: dict[str, Any],
    inp: dict[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """The via/silk/outline/fiducial selection catalogue — key -> identity
    — the object handles resolve against. Built through
    :func:`precis.pcb.selectables.build` over a model assembled like the
    fab renders' (same pads, same furniture), so a handle the box stamped
    means the same object here. ``{}`` when the board is not renderable —
    object handles then resolve as unknown and are reported, never an
    error."""
    if inp is None:
        return {}
    # Local imports: realize/furniture carry shapely, which this module
    # deliberately keeps out of its import-time surface (session.py's own
    # lazy-import posture) — paid only when an object handle is in play.
    from precis.pcb import furniture as pcb_furniture
    from precis.pcb import realize as pcb_realize

    try:
        pads = pcb_realize.pads_for_ir(inp["ir"], inp["layers"], inp["footprints"])
        copper = inp["copper"]
        pads, silk_draws, _warnings, _census, copper = pcb_furniture.board_furniture(
            inp["ir"],
            pads,
            vias=[c for c in copper if c.get("ctype") == "via"],
            instance_sides={
                str(i.get("refdes")): str(i.get("layer") or "top")
                for i in design.get("instances") or []
            },
            copper=copper,
            outline=inp["outline"],
            layer_names=inp["layers"],
            slug=slug,
            clearance_mm=pcb_session.furniture_clearance_mm(inp["stackup"]),
            capability=inp["capability"],
            date=datetime.now(UTC).date().isoformat(),
            polarized=pcb_session.polarized_refdes(design),
        )
        model: dict[str, Any] = {
            "layers": inp["layers"],
            "outline": inp["outline"],
            "copper": copper,
            "pads": pads,
            "silkscreen": silk_draws,
        }
        with store.pcb_view_tx() as conn:
            from precis.pcb.snapshot import capture_raw

            tables = capture_raw(conn, slug)["tables"]
    except Exception as exc:
        log.warning("pcb_argue: object catalogue failed for %s: %s", slug, exc)
        return {}
    objects = pcb_selectables.build(
        model,
        instances=tables["pcb_instances"],
        components=tables["pcb_components"],
        pins=tables["pcb_pins"],
        nets=tables["pcb_nets"],
    )
    return {
        str(o.identity["key"]): dict(o.identity)
        for o in objects
        if o.identity.get("kind") in argue.OBJECT_PREFIXES
    }


def _machinery(
    store: Store,
    ref_id: int,
    inp: dict[str, Any] | None,
) -> tuple[list[str], list[Finding]]:
    """The machinery evidence for the run — ``(notes, findings)``.

    Two DRC sources, both worker-legal: a FRESH placement DRC run (the
    same check the place/route jobs run before routing — pad/copper
    clearance, via keep-outs, courtyard overlap, outline containment),
    and the LAST persisted full geometric run (``view='drc'``'s own
    numbers over routed copper — a run this job does not re-create, named
    by run_id so the answer quotes its freshness). A machinery gap is
    itself evidence: "not checked" is a sayable answer, a silent empty
    list is not."""
    notes: list[str] = []
    findings: list[Finding] = []
    if inp is None:
        notes.append("no board yet — DRC has nothing to check")
    elif inp["capability"] is None:
        notes.append(
            "no fab capability row for this stackup — placement DRC was "
            "not runnable; clearance answers are unchecked"
        )
    else:
        try:
            for f in pcb_session.placement_drc_findings(
                inp["ir"],
                capability=inp["capability"],
                footprints=inp["footprints"],
                fixed_copper=inp["fixed_copper"],
                outline=inp["outline"],
            ):
                findings.append(
                    {
                        "rule": f.rule,
                        "severity": f.severity,
                        "where": f.where,
                        "detail": f.detail,
                        "objects": [dict(o) for o in f.objects],
                        "margin_mm": f.margin_mm,
                        "source": "placement-drc",
                    }
                )
        except Exception as exc:
            notes.append(
                f"the placement DRC run failed ({exc!r}) — treat clearance "
                "answers as unchecked, not clean"
            )
    run_id, rows = store.pcb_drc_findings_latest(ref_id)
    if rows:
        run_tag = (run_id or "?")[:12]
        for r in rows:
            findings.append(
                {
                    "rule": r["rule"],
                    "severity": r["severity"],
                    "where": r.get("location") or "",
                    "detail": r.get("detail") or "",
                    "objects": list(r.get("objects") or []),
                    "margin_mm": r.get("margin_mm"),
                    "source": f"full-drc {run_tag}",
                }
            )
        notes.append(
            f"the last full geometric DRC run ({run_tag}) is quoted "
            "below as full-drc — routed-copper findings as of that run; "
            "placement-drc is the fresh pass"
        )
    return notes, findings


def _word(needle: str, text: str) -> bool:
    return re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", text) is not None


#: ``feature:`` ftype -> finding-vocabulary needles. The checks don't name
#: the feature rows they concern (a ``board_edge_clearance`` finding never
#: says "outline"), so the match is keyword-based: over-inclusive is fine
#: (evidence, not a verdict), under-inclusive would under-report.
_FEATURE_NEEDLES: dict[str, tuple[str, ...]] = {
    "outline": ("outline", "edge"),
    "mounting_hole": ("hole", "npth", "drill"),
    "fiducial": ("fiducial",),
    "testpoint": ("testpoint",),
    "keepout": ("keepout",),
}


def _finding_touches(f: Finding, handle: str) -> bool:
    """Does this finding concern ``handle``? Structured fields first
    (refdes/pin/net/object coordinates), the ``where``/``detail`` text as
    fallback — over-inclusive is fine (it is context, not a verdict),
    under-inclusive would under-report what the machinery said."""
    objects = f.get("objects") or []
    text = f"{f.get('rule') or ''} {f.get('where') or ''} {f.get('detail') or ''}"
    if handle.startswith("net:"):
        net = handle[4:]
        return any(str(o.get("net")) == net for o in objects) or _word(net, text)
    if handle.startswith("feature:"):
        needles = _FEATURE_NEEDLES.get(handle[8:], (handle[8:],))
        low = text.lower()
        return any(n in low for n in needles)
    if handle.startswith("via:"):
        parts = handle.split(":")
        if len(parts) != 3:
            return False
        try:
            x, y = float(parts[1]), float(parts[2])
        except ValueError:
            return False
        return any(
            o.get("x") is not None
            and o.get("y") is not None
            and abs(float(o["x"]) - x) < 0.01
            and abs(float(o["y"]) - y) < 0.01
            for o in objects
        )
    if handle.startswith("silk:"):
        refdes = handle.rsplit(":", 1)[-1]
        return any(str(o.get("refdes")) == refdes for o in objects) or _word(
            refdes, text
        )
    if handle.startswith(("outline:", "fiducial:")):
        # The selectables' segment/ordinal keys mean the same ftype — same
        # needle vocabulary as the feature: handle.
        kind = handle.split(":", 1)[0]
        needles = _FEATURE_NEEDLES.get(kind, (kind,))
        low = text.lower()
        return any(n in low for n in needles)
    refdes, dot, pin = handle.partition(".")
    for o in objects:
        if str(o.get("refdes")) == refdes or str(o.get("component")) == refdes:
            if not dot or str(o.get("pin")) == pin:
                return True
    return _word(handle, text)


def _evidence(
    about: list[str],
    resolved: list[argue.Resolved],
    findings: list[Finding],
    graph: dict[str, Any],
    verdicts: list[dict[str, Any]],
    machinery_notes: list[str],
) -> list[str]:
    """The machinery's numbers for one question — what (b) answers with
    and what (c) is honestly bounded by. DRC findings touching the named
    handles (every error-severity finding when the argument names nothing
    — a whole-design "is this clean?" deserves the error list), distances
    between the parts it names, and the measure verdicts over them."""
    out: list[str] = list(machinery_notes)
    if about:
        touched = [f for f in findings if any(_finding_touches(f, h) for h in about)]
    else:
        touched = [f for f in findings if f["severity"] == "error"]
    for f in touched[:_MAX_DRC_LINES]:
        margin = (
            f" (margin {f['margin_mm']:.3f}mm)"
            if f.get("margin_mm") is not None
            else ""
        )
        out.append(
            f"{f['source']}[{f['severity']}] {f['rule']}: "
            f"{f.get('detail') or ''} — {f.get('where') or ''}{margin}"
        )
    if len(touched) > _MAX_DRC_LINES:
        out.append(f"(…and {len(touched) - _MAX_DRC_LINES} more drc findings)")

    parts = [r.handle for r in resolved if r.kind == "part"]
    for a, b in combinations(sorted(set(parts))[:8], 2):
        try:
            gap = float(pcb_eyes.proximity(graph, a, b)["gap_mm"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(f"part-pair distance {a}<->{b}: {gap:.3f}mm (centroid)")

    named = {r.handle for r in resolved}
    for v in verdicts:
        over = str(v.get("over") or "")
        if about and over and not any(h in over for h in named):
            continue
        if about and not over:
            continue
        verdict = v.get("verdict")
        if verdict in (None, "—"):
            continue
        value = v.get("value")
        val = f"={value}" if value is not None else ""
        goal = v.get("goal")
        out.append(
            f"measure {v.get('metric')} over {over or 'design'}: {verdict} "
            f"(value {val}, goal {goal})"
            + (f" — {v['reason']}" if v.get("reason") else "")
        )
    return out


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pcb_ref_id = int(params["pcb_ref_id"])
    limit = int(params.get("limit") or _DEFAULT_LIMIT)
    only = str(params.get("question") or "").strip() or None

    store = ctx.store
    ref = store.get_ref(kind="pcb", id=pcb_ref_id)
    if ref is None:
        ctx.record_failure(
            f"pcb_argue: no pcb design ref_id={pcb_ref_id}",
            failure_class="infra",
        )
        return
    slug = str(ref.slug or pcb_ref_id)
    title = str(ref.title or slug)
    design = store.pcb_load(pcb_ref_id)
    graph = store.pcb_graph(pcb_ref_id)

    notes = [
        NoteSpec(
            name=str(r["name"]),
            kind=str(r["kind"]),
            body=str(r["body"]),
            re=r["re"],
            about=list(r["about"]),
            origin=str(r["origin"]),
            created_at=r["created_at"],
        )
        for r in store.pcb_notes_list(pcb_ref_id)
    ]
    questions = open_questions(notes)
    if only is not None:
        questions = [q for q in questions if q.name == only]
        if not questions:
            ctx.record_failure(
                f"pcb_argue: {slug} has no open question named {only!r}",
                failure_class="infra",
            )
            return
    questions = questions[:limit]
    if not questions:
        ctx.append_chunk("job_summary", f"{slug}: no open argument questions\n")
        return

    # Shared board inputs, once — the catalogue (only when an object
    # handle is in play) and the machinery both read them.
    inp = _board_inputs(store, pcb_ref_id, design, graph)
    pending = {h for q in questions for h in q.about} | {
        h for q in questions for h in argue.candidates(q.body)
    }
    objects = (
        _catalogue(store, pcb_ref_id, slug, design, inp)
        if any(argue.is_object_handle(h) for h in pending)
        else None
    )

    machinery_notes, findings = _machinery(store, pcb_ref_id, inp)
    verdicts = pcb_eyes.evaluate_measures(
        graph,
        store.pcb_measures_list(pcb_ref_id),
        store.pcb_features_list(pcb_ref_id),
    )
    vitals = argue.vitals(store, pcb_ref_id)

    taken = {n.name for n in notes}
    answered = 0
    failures: list[str] = []
    for q in questions:
        about = list(q.about) + [
            h
            for h in argue.handles_in(q.body, store, pcb_ref_id, objects)
            if h not in q.about
        ]
        resolved, _unknown = argue.resolve(store, pcb_ref_id, about, objects)
        evidence = _evidence(
            about, resolved, findings, graph, verdicts, machinery_notes
        )
        try:
            answer = argue.act(
                title=title,
                text=q.body,
                resolved=resolved,
                vitals=vitals,
                evidence=evidence,
                ref_id=pcb_ref_id,
            )
        except Exception as exc:
            # Same degrade as the web route: the question is on record,
            # the answer is not — one model failure never sinks the run.
            log.warning("pcb_argue: model call failed for %s on %s", q.name, slug)
            failures.append(f"{q.name}: {exc}")
            ctx.append_chunk(
                "job_event", f"pcb_argue: model call failed for {q.name}: {exc!r}"
            )
            continue
        a_name = argue.note_name("answer", answer, taken)
        taken.add(a_name)
        store.pcb_note_insert(
            pcb_ref_id,
            name=a_name,
            kind="answer",
            body=answer,
            re=q.name,
            about=about,
            origin="proposed",
        )
        answered += 1

    if not answered:
        ctx.record_failure(
            f"pcb_argue: every model call failed on {slug} "
            f"({len(failures)}): {'; '.join(failures[:3])}",
            failure_class="infra",
        )
        return
    unanswered = (
        f"; {len(failures)} unanswered (model failure): " + "; ".join(failures[:3])
        if failures
        else ""
    )
    ctx.append_chunk(
        "job_summary",
        f"{slug}: answered {answered} open argument question(s){unanswered} — "
        "each landed as an 'answer' note re its question, origin='proposed'\n",
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("pcb_argue runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="pcb_argue",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
