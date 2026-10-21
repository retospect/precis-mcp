"""Retrieval eval — the gardener-value measure (``graph-gardener-value.md``
slice 2, Reto 2026-10-21).

The question it answers: for one quest's rubric questions, do the graph's
explicit edges get an agent to the chunks that answer them better than
plain retrieval? Two arms run in-process over a :class:`~precis.store.Store`
(never through the MCP — this measures, it does not book):

- **``search``** — the hybrid chunk search the ``search`` verb runs
  (:meth:`precis.store.ChunkStore.search_chunks`, mode ``'hybrid'`` — itself
  degrading to lexical-only when the embedder returns no vector, exactly
  like the verb). Each hit renders as one line ``<chunk-handle>  <excerpt>``
  (the 200-char preview cap of
  :func:`precis.utils.search_merge.block_hits_to_search_hits`), and the arm's
  *retrieved set* is the hit handles in rank order.
- **``fisheye``** — the ``extent='fisheye+1hop'`` render anchored at the
  quest's record handle (:func:`precis.utils.eye_render.render_eye`), one
  render per quest shared by all its questions. The retrieved set is every
  chunk handle the render text surfaces, in render order (first occurrence;
  ``pc``/``fb``/``dc``/… — any decimal handle whose code is a chunk code).
  Note what that measures today: a quest's 1-hop ring lists *record*
  handles (``pa``/``fi``/``me``), so chunk handles surface only where the
  render names them — the honest baseline a gardener pass is meant to move.

Metrics per question:

- **recall@10** — share of the question's gold chunks present in the first
  10 retrieved handles.
- **tokens-to-first-gold** — rendered output characters before the first
  gold chunk handle appears in the arm's output, divided by 4 (the ~4
  chars/token rule of thumb). ``None`` = *not reached*: no gold handle
  appears anywhere in the output (the search arm's ``k`` results bound it;
  the fisheye render bounds itself).

Per arm the report carries: ``n`` questions run, ``errors`` (a question
whose arm raised — excluded from every aggregate, like ``runner.py``'s
transport errors), mean recall@10 with a Wilson 95% CI over the share of
questions at full recall (:func:`~precis.mesh_eval.stats.wilson_interval`),
median tokens-to-first-gold over reached questions, and the not-reached
count. A dated report file is one run; a before/after pair across a
gardener pass is compared by :func:`compare`: **keep** iff an arm's mean
recall@10 rises ≥ 0.05, or its median tokens fall ≥ 10 % with no recall
loss; otherwise **no effect**. The search arm doubles as a control — a
delta there without one in ``fisheye`` is corpus drift, not a gardener
effect, so the overall verdict keeps only on a ``fisheye`` improvement
(falling back to any arm when ``fisheye`` is absent).

Gold sets are JSONL fixtures (``{question, quest, gold_chunks,
labelled_by, at}`` per line) built by
``scripts/llm_eval/build_retrieval_gold.py`` into the gitignored
``scripts/llm_eval/gold_set/local/`` — prod-derived text never enters the
public repo. ``labelled_by`` is ``model:<id>`` until Reto reviews the
labels, then ``reto``. CLI: ``precis mesh retrieval`` /
``precis mesh retrieval-compare`` — ``precis eval`` is the verb-call
executor, so the evals live under the ``mesh`` group with the others.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.mesh_eval.stats import wilson_interval

if TYPE_CHECKING:
    from precis.store.store import Store

#: The two arms the eval runs (``--arm both`` runs each).
ARMS: tuple[str, ...] = ("search", "fisheye")

#: Results the search arm retrieves per question (the ``k`` in the spec).
DEFAULT_K = 30

#: The fisheye arm's extent rung, per the ruling.
FISHEYE_EXTENT = "fisheye+1hop"

#: Excerpt cap for a search hit's rendered line — same as
#: ``block_hits_to_search_hits``' preview, the row an agent scans.
_HIT_EXCERPT = 200

#: Recall window: gold chunks in the first N retrieved count.
_RECALL_WINDOW = 10

#: The keep/no-effect bars a before/after compare applies, per the ruling:
#: mean recall@10 up this much, or median tokens down this share with no
#: recall loss.
_KEEP_RECALL_DELTA = 0.05
_KEEP_TOKEN_FRACTION = 0.10

#: ``<2-char code><digits>`` tokens in a render — candidates for chunk
#: handles, checked through ``handle_registry.parse`` (a record code like
#: ``pa5`` never counts; a suffix form like ``pc10-3`` surfaces as its
#: base ``pc10``, the same chunk).
_HANDLE_TOKEN_RE = re.compile(r"\b[a-z]{2}\d+\b")

_QUEST_HANDLE_RE = re.compile(r"^qu\d+$")
_LABELLED_BY_RE = re.compile(r"^(reto|model:.+)$")


@dataclass(frozen=True, slots=True)
class GoldRecord:
    """One gold row: a rubric question and the chunks that answer it."""

    question: str
    quest: str  # ``qu<ref_id>`` record handle
    gold_chunks: tuple[str, ...]  # chunk handles, e.g. ``pc123``
    labelled_by: str  # ``"model:<id>"`` drafted, ``"reto"`` reviewed
    at: str  # UTC ISO timestamp


@dataclass(frozen=True, slots=True)
class GoldSet:
    """A loaded gold file: the optional ``_meta`` header plus the records."""

    meta: dict[str, Any]
    records: list[GoldRecord]


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """One question's outcome under one arm."""

    question: str
    quest: str
    recall_at_10: float | None
    tokens_to_first_gold: float | None
    not_reached: bool
    retrieved: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ArmReport:
    """One arm's outcome over a gold set."""

    arm: str
    n: int
    errors: int
    mean_recall: float | None
    ci_lo: float
    ci_hi: float
    median_tokens_to_first_gold: float | None
    not_reached: int
    per_question: list[QuestionResult] = field(default_factory=list)


def _require(row: dict[str, Any], key: str, ctx: str) -> Any:
    v = row.get(key)
    if v is None:
        raise BadInput(
            f"retrieval gold: {ctx} is missing field {key!r}",
            next="record shape: {question, quest, gold_chunks, labelled_by, at}",
        )
    return v


def _record_from_row(row: Any, i: int, path: Path) -> GoldRecord:
    ctx = f"{path} line {i}"
    if not isinstance(row, dict):
        raise BadInput(f"retrieval gold: {ctx} is not an object")
    question = _require(row, "question", ctx)
    if not isinstance(question, str) or not question.strip():
        raise BadInput(
            f"retrieval gold: {ctx} field 'question' must be a non-empty string"
        )
    quest = str(_require(row, "quest", ctx))
    if not _QUEST_HANDLE_RE.match(quest):
        raise BadInput(
            f"retrieval gold: {ctx} field 'quest' must be a quest handle "
            f"('qu<ref_id>'), got {quest!r}"
        )
    raw_chunks = _require(row, "gold_chunks", ctx)
    if (
        not isinstance(raw_chunks, list)
        or not raw_chunks
        or not all(isinstance(c, str) for c in raw_chunks)
    ):
        raise BadInput(
            f"retrieval gold: {ctx} field 'gold_chunks' must be a non-empty "
            "list of chunk handles"
        )
    from precis.utils import handle_registry

    for c in raw_chunks:
        parsed = handle_registry.parse(c)
        if parsed is None or not parsed[1]:
            raise BadInput(
                f"retrieval gold: {ctx} field 'gold_chunks' holds {c!r}, "
                "not a chunk handle (e.g. 'pc123')"
            )
    labelled_by = str(_require(row, "labelled_by", ctx))
    if not _LABELLED_BY_RE.match(labelled_by):
        raise BadInput(
            f"retrieval gold: {ctx} field 'labelled_by' must be 'reto' or "
            f"'model:<id>', got {labelled_by!r}"
        )
    at = str(_require(row, "at", ctx))
    try:
        stamp = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BadInput(
            f"retrieval gold: {ctx} field 'at' is not an ISO timestamp: {at!r}"
        ) from exc
    if stamp.tzinfo is None:
        raise BadInput(
            f"retrieval gold: {ctx} field 'at' must carry a UTC offset "
            f"('Z' or '+00:00'), got {at!r}"
        )
    return GoldRecord(
        question=question.strip(),
        quest=quest,
        gold_chunks=tuple(raw_chunks),
        labelled_by=labelled_by,
        at=at,
    )


def load_gold(path: str | Path) -> GoldSet:
    """Load a retrieval gold JSONL file; malformed records refused by field name."""
    p = Path(path)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise BadInput(f"retrieval gold: cannot read {p}: {exc}") from exc
    meta: dict[str, Any] = {}
    records: list[GoldRecord] = []
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BadInput(f"retrieval gold: {p} line {i + 1}: {exc}") from exc
        if isinstance(row, dict) and "_meta" in row:
            meta = dict(row["_meta"])
            continue
        records.append(_record_from_row(row, i + 1, p))
    if not records:
        raise BadInput(f"retrieval gold: {p} holds no records")
    return GoldSet(meta=meta, records=records)


def dump_gold(
    records: list[GoldRecord], meta: dict[str, Any], path: str | Path
) -> Path:
    """Write ``meta`` + ``records`` as JSONL (UTF-8, one object per line)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    meta = {**meta, "set": "retrieval-eval", "n_items": len(records)}
    with p.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": meta}, ensure_ascii=False) + "\n")
        for r in records:
            fh.write(json.dumps(_record_to_json(r), ensure_ascii=False) + "\n")
    return p


def _record_to_json(r: GoldRecord) -> dict[str, Any]:
    return {
        "question": r.question,
        "quest": r.quest,
        "gold_chunks": list(r.gold_chunks),
        "labelled_by": r.labelled_by,
        "at": r.at,
    }


def extract_chunk_handles(text: str) -> list[str]:
    """Every chunk handle ``text`` surfaces, in first-occurrence order.

    A ``<2-char code><digits>`` token counts only when
    :func:`~precis.utils.handle_registry.parse` decodes it as a *chunk*
    handle — record handles (``pa``, ``fi``, ``me``, …) and non-handle
    tokens never enter the retrieved set.
    """
    from precis.utils import handle_registry

    seen: set[str] = set()
    out: list[str] = []
    for m in _HANDLE_TOKEN_RE.finditer(text):
        tok = m.group(0)
        if tok in seen:
            continue
        parsed = handle_registry.parse(tok)
        if parsed is not None and parsed[1]:
            seen.add(tok)
            out.append(tok)
    return out


def _excerpt(text: str, cap: int = _HIT_EXCERPT) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[: cap - 1].rstrip() + "…"


def _search_arm(
    store: Store, question: str, k: int, embedder: Any | None
) -> tuple[str, list[str]]:
    """The hybrid chunk search the ``search`` verb runs, ``k`` hits deep.

    Returns ``(rendered output, retrieved handles in rank order)``. Each
    hit renders as one line — the list an agent scans before drilling.
    """
    from precis.utils import handle_registry
    from precis.utils.embed_query import query_vec_for

    vec = query_vec_for(embedder, question, "hybrid")
    triples = store.chunks.search_chunks(
        q=question,
        query_vec=vec,
        limit=k,
        card_kinds=("card_combined",),
    )
    lines: list[str] = []
    retrieved: list[str] = []
    for block, ref, _score in triples:
        handle = handle_registry.try_format(ref.kind, int(block.id), chunk=True)
        if handle is None:
            continue  # no chunk code — can never match a gold handle
        retrieved.append(handle)
        lines.append(f"{handle}  {_excerpt(block.text)}")
    return "\n".join(lines), retrieved


def _fisheye_arm(store: Store, quest_handle: str) -> tuple[str, list[str]]:
    """The ``fisheye+1hop`` render at the quest handle → surfaced chunk ids."""
    from precis.utils.eye_render import render_eye

    out = render_eye(store, quest_handle, FISHEYE_EXTENT)
    return out, extract_chunk_handles(out)


def _first_gold_offset(output: str, gold: tuple[str, ...]) -> int | None:
    """Char offset of the first gold handle's occurrence in ``output``."""
    best: int | None = None
    for g in gold:
        m = re.search(rf"\b{re.escape(g)}\b", output)
        if m is not None and (best is None or m.start() < best):
            best = m.start()
    return best


def _score_question(
    rec: GoldRecord, output: str, retrieved: list[str]
) -> QuestionResult:
    gold = set(rec.gold_chunks)
    top = set(retrieved[:_RECALL_WINDOW])
    recall = len(gold & top) / len(gold)
    offset = _first_gold_offset(output, rec.gold_chunks)
    return QuestionResult(
        question=rec.question,
        quest=rec.quest,
        recall_at_10=recall,
        tokens_to_first_gold=None if offset is None else offset / 4.0,
        not_reached=offset is None,
        retrieved=tuple(retrieved),
    )


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def run_arm(
    store: Store,
    records: list[GoldRecord],
    *,
    arm: str,
    k: int = DEFAULT_K,
    embedder: Any | None = None,
) -> ArmReport:
    """Run one arm over a gold set's records and aggregate the metrics.

    The ``fisheye`` render is cached per quest handle — every rubric
    question of a quest shares the one anchored render; what differs is
    its gold set.
    """
    if arm not in ARMS:
        raise BadInput(f"retrieval eval: unknown arm {arm!r}", options=list(ARMS))
    eye_cache: dict[str, tuple[str, list[str]]] = {}
    results: list[QuestionResult] = []
    for rec in records:
        try:
            if arm == "search":
                output, retrieved = _search_arm(store, rec.question, k, embedder)
            else:
                if rec.quest not in eye_cache:
                    eye_cache[rec.quest] = _fisheye_arm(store, rec.quest)
                output, retrieved = eye_cache[rec.quest]
            results.append(_score_question(rec, output, retrieved))
        except Exception as exc:
            results.append(
                QuestionResult(
                    question=rec.question,
                    quest=rec.quest,
                    recall_at_10=None,
                    tokens_to_first_gold=None,
                    not_reached=True,
                    retrieved=(),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
    scored = [r for r in results if r.error is None]
    n_err = len(results) - len(scored)
    mean = sum(r.recall_at_10 or 0.0 for r in scored) / len(scored) if scored else None
    n_full = sum(1 for r in scored if (r.recall_at_10 or 0.0) >= 1.0)
    ci_lo, ci_hi = wilson_interval(n_full, len(scored))
    reached = [
        r.tokens_to_first_gold for r in scored if r.tokens_to_first_gold is not None
    ]
    return ArmReport(
        arm=arm,
        n=len(results),
        errors=n_err,
        mean_recall=mean,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        median_tokens_to_first_gold=_median([float(t) for t in reached]),
        not_reached=sum(1 for r in scored if r.not_reached),
        per_question=results,
    )


def run_retrieval(
    store: Store,
    gold: GoldSet,
    *,
    arms: tuple[str, ...] = ARMS,
    k: int = DEFAULT_K,
    embedder: Any | None = None,
    gold_path: str | None = None,
) -> dict[str, Any]:
    """Run the named arms and return the report dict (JSON-serialisable)."""
    reports = [
        run_arm(store, gold.records, arm=a, k=k, embedder=embedder) for a in arms
    ]
    return report_to_json(reports, gold_path=gold_path, k=k)


def report_to_json(
    reports: list[ArmReport], *, gold_path: str | None, k: int
) -> dict[str, Any]:
    """The report-file shape: run meta, then per-arm aggregates + items."""
    return {
        "eval": "retrieval",
        "built_at": datetime.now(UTC).isoformat(),
        "gold": gold_path,
        "k": k,
        "extent": FISHEYE_EXTENT,
        "arms": [_arm_to_json(r) for r in reports],
    }


def _arm_to_json(r: ArmReport) -> dict[str, Any]:
    return {
        "arm": r.arm,
        "n": r.n,
        "errors": r.errors,
        "mean_recall_at_10": r.mean_recall,
        "full_recall_ci": [r.ci_lo, r.ci_hi],
        "median_tokens_to_first_gold": r.median_tokens_to_first_gold,
        "not_reached": r.not_reached,
        "per_question": [
            {
                "question": q.question,
                "quest": q.quest,
                "recall_at_10": q.recall_at_10,
                "tokens_to_first_gold": q.tokens_to_first_gold,
                "not_reached": q.not_reached,
                "retrieved": list(q.retrieved),
                "error": q.error,
            }
            for q in r.per_question
        ],
    }


def _arm_from_json(d: dict[str, Any]) -> ArmReport:
    items = [
        QuestionResult(
            question=str(q.get("question") or ""),
            quest=str(q.get("quest") or ""),
            recall_at_10=q.get("recall_at_10"),
            tokens_to_first_gold=q.get("tokens_to_first_gold"),
            not_reached=bool(q.get("not_reached", False)),
            retrieved=tuple(q.get("retrieved") or ()),
            error=q.get("error"),
        )
        for q in d.get("per_question") or []
    ]
    ci = d.get("full_recall_ci") or [0.0, 1.0]
    return ArmReport(
        arm=str(d.get("arm") or ""),
        n=int(d.get("n") or len(items)),
        errors=int(d.get("errors") or 0),
        mean_recall=d.get("mean_recall_at_10"),
        ci_lo=float(ci[0]),
        ci_hi=float(ci[1]),
        median_tokens_to_first_gold=d.get("median_tokens_to_first_gold"),
        not_reached=int(d.get("not_reached") or 0),
        per_question=items,
    )


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a report file back into the dict shape ``report_to_json`` wrote."""
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BadInput(f"retrieval eval: cannot read report {p}: {exc}") from exc
    if not isinstance(data, dict) or data.get("eval") != "retrieval":
        raise BadInput(f"retrieval eval: {p} is not a retrieval report")
    return data


def _report_arms(data: dict[str, Any]) -> dict[str, ArmReport]:
    return {a.arm: a for a in (_arm_from_json(d) for d in data.get("arms") or [])}


def compare(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Compare two dated report dicts across a gardener pass.

    Per arm shared by both reports: ``keep`` when mean recall@10 rose
    ≥ 0.05, or median tokens-to-first-gold fell ≥ 10 % with no recall
    loss; ``no effect`` otherwise. The overall verdict keeps only on a
    ``fisheye`` improvement — ``search`` is the control arm (it reads no
    edges), so a search-only delta is corpus drift, not gardener value.
    With no ``fisheye`` arm in both, any arm's keep carries the verdict.
    """
    ba, aa = _report_arms(before), _report_arms(after)
    arms: dict[str, Any] = {}
    for name in sorted(set(ba) & set(aa)):
        b, a = ba[name], aa[name]
        recall_delta = None
        if a.mean_recall is not None and b.mean_recall is not None:
            recall_delta = a.mean_recall - b.mean_recall
        token_delta_pct = None
        if a.median_tokens_to_first_gold is not None and b.median_tokens_to_first_gold:
            token_delta_pct = (
                b.median_tokens_to_first_gold - a.median_tokens_to_first_gold
            ) / b.median_tokens_to_first_gold
        recall_lost = recall_delta is not None and recall_delta < 0
        keep = (recall_delta is not None and recall_delta >= _KEEP_RECALL_DELTA) or (
            token_delta_pct is not None
            and token_delta_pct >= _KEEP_TOKEN_FRACTION
            and not recall_lost
        )
        arms[name] = {
            "verdict": "keep" if keep else "no effect",
            "recall_at_10": {
                "before": b.mean_recall,
                "after": a.mean_recall,
                "delta": recall_delta,
            },
            "median_tokens_to_first_gold": {
                "before": b.median_tokens_to_first_gold,
                "after": a.median_tokens_to_first_gold,
                "delta_pct": token_delta_pct,
            },
            "not_reached": {"before": b.not_reached, "after": a.not_reached},
        }
    if "fisheye" in arms:
        verdict = arms["fisheye"]["verdict"]
    else:
        verdict = (
            "keep"
            if any(v["verdict"] == "keep" for v in arms.values())
            else "no effect"
        )
    return {"verdict": verdict, "arms": arms}


def render_report(report: dict[str, Any]) -> str:
    """Human-readable per-arm table: recall + CI, tokens, not-reached."""
    lines: list[str] = []
    for d in report.get("arms") or []:
        r = _arm_from_json(d)
        head = f"{r.arm}: n={r.n} errors={r.errors}"
        lines.append(head)
        if r.mean_recall is not None:
            lines.append(
                f"  recall@10 (mean) {r.mean_recall:.3f}   "
                f"full-recall share [{r.ci_lo:.3f}, {r.ci_hi:.3f}] (Wilson 95)"
            )
        med = r.median_tokens_to_first_gold
        lines.append(
            "  tokens to first gold (median, reached only): "
            + (f"{med:.0f}" if med is not None else "—")
            + f"   not reached: {r.not_reached}"
        )
    return "\n".join(lines)


def render_comparison(cmp: dict[str, Any]) -> str:
    """Human-readable compare output: the verdict, then per-arm deltas."""
    lines = [f"verdict: {cmp['verdict']}"]
    for name, a in cmp.get("arms", {}).items():
        r = a["recall_at_10"]
        t = a["median_tokens_to_first_gold"]
        rd = f"{r['delta']:+.3f}" if r["delta"] is not None else "—"
        td = f"{t['delta_pct'] * 100:+.0f}%" if t["delta_pct"] is not None else "—"
        lines.append(
            f"  {name}: {a['verdict']} — recall@10 {rd}, "
            f"median tokens {td}, not-reached "
            f"{a['not_reached']['before']}→{a['not_reached']['after']}"
        )
    return "\n".join(lines)


__all__ = [
    "ARMS",
    "DEFAULT_K",
    "FISHEYE_EXTENT",
    "ArmReport",
    "GoldRecord",
    "GoldSet",
    "QuestionResult",
    "compare",
    "dump_gold",
    "extract_chunk_handles",
    "load_gold",
    "load_report",
    "render_comparison",
    "render_report",
    "report_to_json",
    "run_arm",
    "run_retrieval",
]
