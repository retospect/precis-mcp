"""Raw benchmark-log reader (gr624301 — the ``raw-benchmark-log-reader``).

``scripts/bench report`` aggregates a run into per-arm tables; this is the
reader for the *raw* log: one block per task x arm row with its per-call
token log, selectable by arm, task and source and paged with
``--offset`` / ``--limit``. It exists because a full run's results file is
too large to read into an agent's context whole: digesting runs #1–#3
truncated past their first few arms. Here every arm is reachable in bounded
slices — one arm's rows, one task across arms, or a page of the long table —
and the continuation line names the next slice, the same ``Next:``
affordance the product's renders use.

Two surfaces share the row block (:func:`row_block`): the
``scripts/bench raw`` CLI, and the quest tick's ``read`` channel — a
``bench:<stem>`` read address resolves through :func:`resolve_results`
against the results dir (:func:`precis.bench.runner.default_results_dir`,
``PRECIS_BENCH_RESULTS``-overridable) and renders the same bounded slices
into next tick's Requested reads (gr618517 — the CLI alone was unreachable
from a model that outputs one JSON payload).

The same read channel also reaches the run's raw MCP-log records — the
``tool_calls`` ledger rows its dispatches wrote — through a reserved
``calls`` section (:func:`fetch_ledger`, :func:`call_line`). The ledger's
``result_tokens_est`` is the ``mcp_call`` line's ``response_tokens_est``
(migration 0297, gr604903): the independent per-call estimate the
results file's own ``tokens`` figures get spot-checked against — the
caller-side estimate alone verifies nothing.

Runs written before the per-call log (results schema 1) carry no
``call_log`` on their rows; their summary lines still print, without call
lines. Runs written before schema 3 carry no ``header.tool_calls`` tag;
the ``calls`` section then falls back to the run's ``started_at``–
``ended_at`` window (untagged rows only — labelled, since a shared DB
can interleave unrelated calls).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from precis.utils.next_block import render_next_section

DEFAULT_LIMIT = 50

#: ``tool_calls`` rows per ``calls``-section page — the window one spot
#: check reads at a time.
LEDGER_PAGE = 60


def select_rows(
    results: Mapping[str, Any],
    *,
    arms: Sequence[str] | None = None,
    task: str | None = None,
    source: str | None = None,
    abstention: bool = False,
) -> list[Mapping[str, Any]]:
    """The rows matching the selectors, in file order."""
    rows = list(results["rows"])
    if arms:
        rows = [r for r in rows if r["arm"] in arms]
    if task is not None:
        rows = [r for r in rows if r["task"] == task]
    if source is not None:
        rows = [r for r in rows if r["source"] == source]
    if abstention:
        rows = [r for r in rows if r["abstention"]]
    return rows


def _outcome(r: Mapping[str, Any]) -> str:
    if r.get("abstention"):
        return "abstention"
    if r.get("applicable") is False:
        return "n/a"
    if r.get("found") is True:
        return "found"
    return "CENSORED" if r.get("censored") else "missed"


def row_header(r: Mapping[str, Any]) -> str:
    """The one-line summary of one task x arm row."""
    bits = [
        f"task {r['task']}",
        f"arm {r['arm']}",
        str(r.get("type", "?")),
        _outcome(r),
        f"tok→gold {r['tokens_to_first_gold']}",
        f"total {r['tokens_total']}",
        f"calls {r['calls']}",
    ]
    if r.get("verbatim_opens"):
        bits.append(f"opens {r['verbatim_opens']}")
    if r.get("refusals"):
        bits.append(f"refusals {r['refusals']}")
    if r.get("errors"):
        bits.append(f"errors {r['errors']}")
    if r.get("recall_at_5") is not None:
        bits.append(f"recall@5 {r['recall_at_5']:.3f}")
    return " · ".join(bits)


def row_block(r: Mapping[str, Any]) -> str:
    """One task x arm row's raw block — the :func:`row_header` summary
    line, then its per-call token log. Shared by ``raw_markdown`` and the
    quest ``bench:`` read's pages, so a tick sees the log in exactly the
    CLI's shape."""
    lines = [row_header(r)]
    for c in r.get("call_log") or ():
        flag = " (refused)" if c.get("refusal") else ""
        lines.append(f"  {c['n']}. {c['verb']} {c['what']} — {c['tokens']:g} tok{flag}")
    return "\n".join(lines)


def arm_names(results: Mapping[str, Any]) -> list[str]:
    """Distinct arm names in file order — one ``bench:`` read's ``section``
    vocabulary (gr618517)."""
    seen: dict[str, None] = {}
    for r in results["rows"]:
        seen.setdefault(str(r["arm"]))
    return list(seen)


def task_ids(results: Mapping[str, Any]) -> list[str]:
    """Distinct task ids in file order — the other ``section`` vocabulary."""
    seen: dict[str, None] = {}
    for r in results["rows"]:
        seen.setdefault(str(r["task"]))
    return list(seen)


def list_results_stems(results_dir: Path) -> list[str]:
    """Stems of the ``*.json`` files in ``results_dir``, newest first —
    the ``bench:<stem>`` address book a missed read lists (gr618517)."""
    if not results_dir.is_dir():
        return []
    stems = [
        (p.stat().st_mtime, p.stem) for p in results_dir.glob("*.json") if p.is_file()
    ]
    return [stem for _mtime, stem in sorted(stems, reverse=True)]


def resolve_results(results_dir: Path, name: str) -> Path | None:
    """``bench:<name>`` → its results file, or ``None``. ``name`` is the
    filename stem (a trailing ``.json`` is tolerated); anything that would
    address outside ``results_dir`` — a separator, a dot name — resolves
    to ``None``, never a path."""
    stem = name.strip()
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem:
        return None
    path = results_dir / f"{stem}.json"
    return path if path.is_file() else None


def raw_markdown(
    results: Mapping[str, Any],
    *,
    arms: Sequence[str] | None = None,
    task: str | None = None,
    source: str | None = None,
    abstention: bool = False,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """The raw log in one bounded slice: a header line naming the window, one
    block per matching row (summary line, then its per-call token log), and a
    ``Next:`` continuation line when rows remain past the window."""
    if offset < 0 or limit < 1:
        raise ValueError("offset must be >= 0 and limit >= 1")
    sel = select_rows(
        results, arms=arms, task=task, source=source, abstention=abstention
    )
    h = results["header"]
    caller = h.get("caller", {}).get("name", "?")
    end = min(offset + limit, len(sel))
    out = [
        f"# raw log {h.get('set', '?')} (caller {caller}) — rows {offset}-{end} "
        f"of {len(sel)}"
    ]
    for r in sel[offset:end]:
        out.append("")
        out.append(row_block(r))
    if end < len(sel):
        out.append(
            render_next_section(
                [
                    (
                        f"scripts/bench raw <results.json> --offset {end} "
                        f"--limit {limit}",
                        f"read rows {end}-{min(end + limit, len(sel))} of {len(sel)}",
                    )
                ]
            )
        )
    return "\n".join(out) + "\n"


# ── the run's raw MCP-log records (tool_calls, migration 0297) ──────────

_LEDGER_COLS = (
    "call_id",
    "ts",
    "verb",
    "kind",
    "outcome",
    "error_type",
    "latency_ms",
    "result_bytes",
    "result_tokens_est",
    "arg_sample",
)


def fetch_ledger(
    store: Any,
    *,
    source: str | None = None,
    started_at: str | None = None,
    ended_at: str | None = None,
    offset: int = 0,
    limit: int = LEDGER_PAGE,
) -> tuple[list[Mapping[str, Any]], int]:
    """One bounded window of a bench run's ``tool_calls`` rows — oldest
    first — plus the run's total.

    Correlation: a schema-3 results file names its tag in
    ``header.tool_calls.source`` (``runner.run_cli`` sets
    ``PRECIS_CURRENT_SOURCE`` for the measured matrix, so every dispatch
    it made carries ``tool_calls.source = 'bench:<stem>'``). An older
    file falls back to its ``started_at``–``ended_at`` window over the
    UNTAGGED rows (``source IS NULL`` — rows a tagged run owns never leak
    into a windowed read; unrelated untagged calls in the window still
    can, and the read says so). No correlation at all answers ``([], 0)``.
    """
    params: tuple[str, ...]
    if source:
        where, params = "source = %s", (source,)
    elif started_at and ended_at:
        where, params = (
            "ts >= %s AND ts <= %s AND source IS NULL",
            (started_at, ended_at),
        )
    else:
        return [], 0
    cols = ", ".join(_LEDGER_COLS)
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT count(*) FROM tool_calls WHERE {where}", params
        ).fetchone()
        total = int(row[0]) if row else 0
        rows = conn.execute(
            f"SELECT {cols} FROM tool_calls WHERE {where} "
            "ORDER BY call_id LIMIT %s OFFSET %s",
            (*params, limit, offset),
        ).fetchall()
    return [dict(zip(_LEDGER_COLS, r, strict=True)) for r in rows], total


def call_line(rec: Mapping[str, Any]) -> str:
    """One ``tool_calls`` row as a one-line log record — the raw form of a
    results-file ``call_log`` entry: the same call shape (verb, kind, the
    bounded arg sample), but the token figure is the ledger's own
    ``result_tokens_est`` — the ``mcp_call`` line's ``response_tokens_est``
    — not the caller-side estimate the results file records."""
    head = f"{rec['call_id']} {rec['verb']}"
    if rec.get("kind"):
        head += f" {rec['kind']}"
    head += f" {rec['outcome']}"
    if rec.get("error_type"):
        head += f" ({rec['error_type']})"
    est = rec.get("result_tokens_est")
    bits = [f"{est} tok" if est is not None else "- tok"]
    if rec.get("result_bytes") is not None:
        bits.append(f"{rec['result_bytes']}B")
    if rec.get("latency_ms") is not None:
        bits.append(f"{rec['latency_ms']}ms")
    ts = rec.get("ts")
    if ts is not None:
        bits.append(ts.strftime("%H:%M:%SZ") if hasattr(ts, "strftime") else str(ts))
    sample = rec.get("arg_sample")
    if sample:
        s = json.dumps(sample, ensure_ascii=False, sort_keys=True)
        bits.append(s[:120] + ("…" if len(s) > 120 else ""))
    return f"  {head} — {' · '.join(bits)}"
