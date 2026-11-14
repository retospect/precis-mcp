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

The reserved ``bench:index`` address (``INDEX_STEM``; gr660023) renders
the run index — one :func:`index_line` per held file with the stem plus
its dates, set, caller and the ``header.todo`` handle the run was
launched under (schema 5+) — and its ``section`` filters the listing by
normalised substring over the line or the raw file text: the
search-by-date or by-todo-reference the quest asked for so it can name
the right stem. A ``bench:`` address that is a unique stem substring
(:func:`match_stems`) resolves to that run outright.
"""

from __future__ import annotations

import json
import re
import shlex
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
    if r.get("gold_rank_page") is not None:
        bits.append(f"gold@page {r['gold_rank_page']}")
    if r.get("gold_rank_100") is not None:
        bits.append(f"gold@100 {r['gold_rank_100']}")
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


# ── the run index (gr660023) ──────────────────────────────────────────

#: The reserved ``bench:`` read address that renders the run index — one
#: line per held results file, so a tick can NAME the right ``bench:<stem>``
#: instead of guessing. Checked before stem resolution, the way the
#: ``calls`` section shadows a task id: a results file literally named
#: ``index.json`` is unreachable by address (none exists — stems mint as
#: ``<day>Z-<sha>-<set>-<caller>``).
INDEX_STEM = "index"

#: The results-retention policy in one line, rendered with the run index
#: so the listing itself answers "does a run absent here exist anywhere"
#: (gr660413 — a probe returning zero held runs could not say whether
#: runs #2/#3 survived on some other checkout; now an empty or
#: filtered-empty listing still carries the policy). The long form is
#: ``bench/results/README.md`` `## Retention`; the needs-apply answer
#: (``precis.quest.needs._bench_retention_hint``) quotes the same facts.
RETENTION_LINE = (
    "retention: results files live only on the host+checkout that ran "
    "them — never uploaded, archived or pruned; a run not listed "
    "survives only where it ran (or is gone)"
)


def _norm(needle: str) -> str:
    """A search needle's matchable form — lowercase, alphanumerics only —
    so ``2026-11-09``, ``20261109`` and ``2026/11/09`` are the same date,
    and ``td 604857`` hits ``td604857`` (gr660023)."""
    return re.sub(r"[^a-z0-9]+", "", needle.lower())


def match_stems(results_dir: Path, needle: str) -> list[str]:
    """Held stems containing ``needle`` (normalised substring), newest
    first — the forgiving half of a ``bench:`` address: a date fragment
    or a caller/set word names a run without the full stem. A trailing
    ``.json`` is tolerated, as in :func:`resolve_results`."""
    n = _norm(needle.removesuffix(".json"))
    if not n:
        return []
    return [s for s in list_results_stems(results_dir) if n in _norm(s)]


def run_index(results_dir: Path) -> list[dict[str, Any]]:
    """One index entry per held results file, newest first — the data
    behind the ``bench:index`` read and ``scripts/bench index``. Each
    entry carries the ``stem`` (its ``bench:<stem>`` address) plus the
    header facts a search needs to pick a run: ``started_at``/``ended_at``,
    ``set``, ``caller``, ``schema``, ``precis_sha``/``git_dirty``, the
    ``todo`` handle the run was launched under (schema 5+), and its
    row/arm/task counts. An unreadable file still lists — the stem is
    the address — with ``readable`` False."""
    out: list[dict[str, Any]] = []
    if not results_dir.is_dir():
        return out
    for p in results_dir.glob("*.json"):
        if not p.is_file():
            continue
        e: dict[str, Any] = {
            "stem": p.stem,
            "path": p,
            "mtime": p.stat().st_mtime,
            "readable": False,
        }
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            h = data["header"]
            rows = data["rows"]
            if not isinstance(h, Mapping) or not isinstance(rows, list):
                raise ValueError("malformed results file")
            if not all(isinstance(r, Mapping) for r in rows):
                raise ValueError("malformed results rows")
            n_arms = len({r.get("arm") for r in rows})
            n_tasks = len({r.get("task") for r in rows})
        except Exception:
            e["note"] = "unreadable results file"
        else:
            caller = h.get("caller")
            e.update(
                readable=True,
                set=h.get("set"),
                caller=caller.get("name") if isinstance(caller, Mapping) else None,
                schema=h.get("schema"),
                precis_sha=h.get("precis_sha"),
                git_dirty=h.get("git_dirty"),
                todo=h.get("todo"),
                started_at=h.get("started_at"),
                ended_at=h.get("ended_at"),
                n_rows=len(rows),
                n_arms=n_arms,
                n_tasks=n_tasks,
            )
        out.append(e)
    out.sort(key=lambda e: (str(e.get("started_at") or ""), e["mtime"]), reverse=True)
    return out


def index_line(e: Mapping[str, Any]) -> str:
    """One run's index line — the stem (its ``bench:<stem>`` address),
    then the facts a reader picks a run by."""
    if not e.get("readable"):
        return f"{e['stem']} — ({e.get('note') or 'unreadable results file'})"
    bits = [
        f"set {e.get('set') or '?'}",
        f"caller {e.get('caller') or '?'}",
        f"{e.get('started_at') or '?'} → {e.get('ended_at') or '?'}",
        f"schema {e.get('schema') if e.get('schema') is not None else '?'}",
        f"{e.get('n_rows', 0)} rows",
        f"{e.get('n_arms', 0)} arms",
        f"{e.get('n_tasks', 0)} tasks",
    ]
    if e.get("todo"):
        bits.append(str(e["todo"]))
    if e.get("precis_sha"):
        bits.append(f"sha {str(e['precis_sha'])[:9]}")
    if e.get("git_dirty"):
        bits.append("dirty")
    return f"{e['stem']} — {' · '.join(bits)}"


def file_mentions(path: Path, needle: str) -> bool:
    """Normalised substring scan of a results file's raw JSON — how a
    ``bench:index`` filter finds a run by what the file *says* (a
    ``header.todo`` handle, a gripe a ``known_defect`` cites), not only
    by what its stem is called."""
    n = _norm(needle)
    if not n:
        return False
    try:
        return n in _norm(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return False


def index_lines(results_dir: Path, *, match: str | None = None) -> list[str]:
    """The formatted index lines the ``bench:index`` read renders —
    every held run, or with ``match`` only the runs whose index line or
    file text contains it (search-by-date or by-reference, gr660023)."""
    runs = run_index(results_dir)
    n = _norm(match) if match is not None else ""
    out: list[str] = []
    for e in runs:
        line = index_line(e)
        if n and n not in _norm(line) and not file_mentions(e["path"], match or ""):
            continue
        out.append(line)
    return out


def index_markdown(
    results_dir: Path,
    *,
    match: str | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> str:
    """The run index in one bounded slice — the ``scripts/bench index``
    render: a header line naming the window, one :func:`index_line` per
    matching run, the :data:`RETENTION_LINE` footer (an empty listing
    still says what absence means, gr660413), and a ``Next:``
    continuation when runs remain past the window (the same paging
    convention as :func:`raw_markdown`)."""
    if offset < 0 or limit < 1:
        raise ValueError("offset must be >= 0 and limit >= 1")
    lines = index_lines(results_dir, match=match)
    scope = f" matching {match!r}" if match else ""
    end = min(offset + limit, len(lines))
    out = [
        f"# bench run index{scope} — runs {offset}-{end} of {len(lines)} "
        f"({results_dir})"
    ]
    out.extend(lines[offset:end])
    out.append(RETENTION_LINE)
    if end < len(lines):
        call = "scripts/bench index"
        if match:
            call += f" --match {shlex.quote(match)}"
        call += f" --offset {end} --limit {limit}"
        out.append(
            render_next_section(
                [
                    (
                        call,
                        f"read runs {end}-{min(end + limit, len(lines))} of {len(lines)}",
                    )
                ]
            )
        )
    return "\n".join(out) + "\n"


def raw_markdown(
    results: Mapping[str, Any],
    *,
    arms: Sequence[str] | None = None,
    task: str | None = None,
    source: str | None = None,
    abstention: bool = False,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    results_path: Path | str | None = None,
) -> str:
    """The raw log in one bounded slice: a header line naming the window, one
    block per matching row (summary line, then its per-call token log), and a
    ``Next:`` continuation line when rows remain past the window. The hinted
    call names ``results_path`` and replays the active selectors, since
    ``offset`` pages the *filtered* selection — without them it would page
    the unfiltered table at the filtered ``end``."""
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
        target = shlex.quote(str(results_path)) if results_path else "<results.json>"
        call = f"scripts/bench raw {target}"
        for a in arms or ():
            call += f" --arm {shlex.quote(str(a))}"
        if task is not None:
            call += f" --task {shlex.quote(task)}"
        if source is not None:
            call += f" --source {shlex.quote(source)}"
        if abstention:
            call += " --abstention"
        call += f" --offset {end} --limit {limit}"
        out.append(
            render_next_section(
                [
                    (
                        call,
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
    — not the caller-side estimate the results file records. Both
    response-size fields render by name (``-`` when the row carries
    none), so the read itself confirms they are populated (gr618518)."""
    head = f"{rec['call_id']} {rec['verb']}"
    if rec.get("kind"):
        head += f" {rec['kind']}"
    head += f" {rec['outcome']}"
    if rec.get("error_type"):
        head += f" ({rec['error_type']})"
    est = rec.get("result_tokens_est")
    r_bytes = rec.get("result_bytes")
    bits = [
        f"result_bytes={r_bytes if r_bytes is not None else '-'}",
        f"result_tokens_est={est if est is not None else '-'}",
    ]
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
