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

Runs written before the per-call log (results schema 1) carry no
``call_log`` on their rows; their summary lines still print, without call
lines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from precis.utils.next_block import render_next_section

DEFAULT_LIMIT = 50


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
        out.append(row_header(r))
        for c in r.get("call_log") or ():
            flag = " (refused)" if c.get("refusal") else ""
            out.append(
                f"  {c['n']}. {c['verb']} {c['what']} — {c['tokens']:g} tok{flag}"
            )
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
