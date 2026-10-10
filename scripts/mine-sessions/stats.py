#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""Scoreboard: the one-screen read that starts a `surface-review` pass,
before anyone opens a single evidence card.

Renders `out/scoreboard.md` (for the main loop to read) and
`out/scoreboard.json` (`schema_version`-carrying, so pass N's numbers are
comparable to pass N-1's — the whole reason this exists instead of a fresh
scratch script per pass, per `docs/mine-sessions/README.md`).

Error-rate arithmetic here reuses `detect.exec_class` rather than
re-deriving the blocked/rejected/hang/zero/ok split — see that function's
docstring for why the split has to happen before any rate is trustworthy.
Byte percentiles reuse `detect.percentile` for the same reason: one
implementation, so a future change to either rule can't silently diverge
between the two files.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import detect
import outdir
from redact import scrub
from schema import Event

SCHEMA_VERSION = 1


def _date_range(events: Sequence[Event]) -> tuple[str | None, str | None]:
    tss = sorted(e.ts for e in events if e.ts)
    return (tss[0], tss[-1]) if tss else (None, None)


def _surface_quality_events(events: Sequence[Event]) -> list[Event]:
    """precis calls the error rate is fair to compute over: everything
    except the ``blocked``/``rejected``/``hang`` harness noise (see
    ``detect.exec_class``).

    Payload-free corpora are **kept**. `exec_class` returns ``None`` for
    them because it cannot read a result that was never recorded — but the
    ledger still records ``outcome``/``error_type`` faithfully, and it is by
    far the widest denominator we have (34k fleet-wide rows against ~1.4k
    local ones). Dropping it inverts the picture: on the first real pass it
    took ``patent`` from a 16.8% error rate to 0.0%, because every one of
    those errors was fleet-side. The harness noise classes can only arise in
    a local transcript anyway, so there is nothing to exclude there.
    """
    out = []
    for ev in events:
        if not detect.is_precis(ev):
            continue
        if not detect.has_payload(ev):
            out.append(ev)
            continue
        if detect.exec_class(ev) in ("ok", "zero"):
            out.append(ev)
    return out


def _corpus_census(events: Sequence[Event]) -> dict[str, Any]:
    by_corpus: dict[str, dict[str, Any]] = {}
    for corpus, evs in _group(events, lambda e: e.corpus).items():
        by_corpus[corpus] = {
            "events": len(evs),
            "sessions": len({e.session for e in evs}),
        }
    lo, hi = _date_range(events)
    return {
        "total_events": len(events),
        "total_sessions": len({e.session for e in events}),
        "date_range": [lo, hi],
        # schema.Event carries no parse-failure marker of its own; a corpus
        # that dropped lines during extraction reports that separately
        # (extract.py's own log), not here. None means "not tracked in this
        # event set", not "zero failures".
        "parse_failures": None,
        "by_corpus": by_corpus,
    }


def _group(events: Sequence[Event], key: Any) -> dict[Any, list[Event]]:
    out: dict[Any, list[Event]] = defaultdict(list)
    for ev in events:
        out[key(ev)].append(ev)
    return out


def _error_rate(evs: Sequence[Event]) -> float:
    quality = _surface_quality_events(evs)
    if not quality:
        return 0.0
    return sum(1 for e in quality if e.is_error) / len(quality)


def _per_verb(events: Sequence[Event]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for verb, evs in _group(
        [e for e in events if detect.is_precis(e)], lambda e: e.verb
    ).items():
        # Byte percentiles only over corpora that actually carry a payload —
        # the ledger and llmlog have none by design, and including their
        # zeros drags every p50 to 0, which reads as "this verb returns
        # nothing" rather than "most of these rows can't answer the
        # question". `payload_calls` is reported so the denominator is
        # visible rather than implied.
        sized = [e for e in evs if detect.has_payload(e)]
        sizes = sorted(e.result_bytes for e in sized)
        latencies = [e.latency_ms for e in evs if e.latency_ms is not None]
        out[verb] = {
            "calls": len(evs),
            "payload_calls": len(sized),
            "error_rate": round(_error_rate(evs), 4),
            "p50_bytes": round(detect.percentile(sizes, 0.5), 1),
            "p95_bytes": round(detect.percentile(sizes, 0.95), 1),
            "mean_latency_ms": round(sum(latencies) / len(latencies), 1)
            if latencies
            else None,
        }
    return out


def _per_kind_top20(events: Sequence[Event]) -> list[dict[str, Any]]:
    groups = _group(
        [e for e in events if detect.is_precis(e) and e.kind], lambda e: e.kind
    )
    rows = [
        {"kind": kind, "calls": len(evs), "error_rate": round(_error_rate(evs), 4)}
        for kind, evs in groups.items()
    ]
    rows.sort(key=lambda r: r["calls"], reverse=True)
    return rows[:20]


def _usage_num(usage: dict[str, Any], key: str) -> int:
    val = usage.get(key)
    if isinstance(val, bool):
        return 0
    if isinstance(val, int):
        return val
    if isinstance(val, dict):
        return sum(
            v for v in val.values() if isinstance(v, int) and not isinstance(v, bool)
        )
    return 0


def _tokens(events: Sequence[Event]) -> dict[str, Any]:
    assistant_turns = [e for e in events if e.tool is None and e.usage]
    input_total = sum(_usage_num(e.usage, "input_tokens") for e in assistant_turns)
    output_total = sum(_usage_num(e.usage, "output_tokens") for e in assistant_turns)
    cache_read_total = sum(
        _usage_num(e.usage, "cache_read_input_tokens") for e in assistant_turns
    )
    cache_creation_total = sum(
        _usage_num(e.usage, "cache_creation_input_tokens") for e in assistant_turns
    )
    # Codex input_tokens already includes cached input; Claude's does not.
    denom = input_total + sum(
        _usage_num(e.usage, "cache_read_input_tokens")
        for e in assistant_turns
        if e.corpus != "codex"
    )
    cache_read_ratio = cache_read_total / denom if denom else 0.0

    per_session: dict[str, int] = defaultdict(int)
    for e in assistant_turns:
        per_session[e.session] += _usage_num(e.usage, "input_tokens") + _usage_num(
            e.usage, "output_tokens"
        )
    top_sessions = sorted(per_session.items(), key=lambda kv: kv[1], reverse=True)[:10]

    tool_events = [e for e in events if e.tool not in (None, "__user__")]
    total_tool_bytes = sum(e.result_bytes for e in tool_events)
    precis_bytes = sum(e.result_bytes for e in tool_events if detect.is_precis(e))
    precis_share = precis_bytes / total_tool_bytes if total_tool_bytes else 0.0

    return {
        "input_total": input_total,
        "output_total": output_total,
        "cache_read_total": cache_read_total,
        "cache_creation_total": cache_creation_total,
        "cache_read_ratio": round(cache_read_ratio, 4),
        "top_sessions": [{"session": scrub(s), "tokens": t} for s, t in top_sessions],
        "precis_result_bytes_share": round(precis_share, 4),
        "precis_result_bytes_total": precis_bytes,
        "total_tool_result_bytes": total_tool_bytes,
    }


def _detour_census(events: Sequence[Event]) -> dict[str, Any]:
    hits = [
        e
        for e in events
        if e.tool == "Bash" and detect.DETOUR_CENSUS_RE.search(e.arg_digest or "")
    ]
    return {"count": len(hits), "bytes_total": sum(e.result_bytes for e in hits)}


def _exec_class_mix(events: Sequence[Event]) -> dict[str, int]:
    mix: dict[str, int] = defaultdict(int)
    for ev in events:
        cls = detect.exec_class(ev)
        if cls:
            mix[cls] += 1
    return dict(mix)


def build_scoreboard(events: Sequence[Event]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "corpus_census": _corpus_census(events),
        "per_verb": _per_verb(events),
        "per_kind_top20": _per_kind_top20(events),
        "tokens": _tokens(events),
        "detour_census": _detour_census(events),
        "exec_class_mix": _exec_class_mix(events),
    }


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def render_markdown(sb: dict[str, Any]) -> str:
    lines: list[str] = ["# mine-sessions scoreboard", ""]

    cc = sb["corpus_census"]
    lines.append(
        f"census: {cc['total_events']} events, {cc['total_sessions']} sessions, "
        f"{cc['date_range'][0] or '?'}..{cc['date_range'][1] or '?'}"
        + (
            f", parse_failures={cc['parse_failures']}"
            if cc["parse_failures"] is not None
            else ""
        )
    )
    lines.append(
        "  by corpus: "
        + ", ".join(f"{k}={v['events']}" for k, v in sorted(cc["by_corpus"].items()))
    )
    lines.append("")

    lines.append("## per-verb")
    lines.append("verb | calls | w/payload | err% | p50B | p95B | mean_lat_ms")
    lines.append("---|---|---|---|---|---|---")
    for verb, row in sorted(sb["per_verb"].items(), key=lambda kv: -kv[1]["calls"]):
        lines.append(
            f"{verb} | {row['calls']} | {row['payload_calls']} | "
            f"{_fmt_pct(row['error_rate'])} | "
            f"{row['p50_bytes']:.0f} | {row['p95_bytes']:.0f} | "
            f"{row['mean_latency_ms'] if row['mean_latency_ms'] is not None else '-'}"
        )
    lines.append("")

    lines.append("## per-kind (top 20 by calls)")
    lines.append("kind | calls | err%")
    lines.append("---|---|---")
    for row in sb["per_kind_top20"]:
        lines.append(f"{row['kind']} | {row['calls']} | {_fmt_pct(row['error_rate'])}")
    lines.append("")

    tk = sb["tokens"]
    lines.append("## tokens")
    lines.append(
        f"input={tk['input_total']} output={tk['output_total']} "
        f"cache_read={tk['cache_read_total']} cache_creation={tk['cache_creation_total']} "
        f"cache_read_ratio={_fmt_pct(tk['cache_read_ratio'])}"
    )
    lines.append(
        f"precis result_bytes share of tool-result bytes: "
        f"{_fmt_pct(tk['precis_result_bytes_share'])} "
        f"({tk['precis_result_bytes_total']}/{tk['total_tool_result_bytes']})"
    )
    lines.append(
        "top sessions by tokens: "
        + ", ".join(f"{s['session']}={s['tokens']}" for s in tk["top_sessions"])
    )
    lines.append("")

    dc = sb["detour_census"]
    lines.append(
        f"## detour census: {dc['count']} Bash calls, {dc['bytes_total']} bytes total"
    )
    lines.append("")

    mix = sb["exec_class_mix"]
    total = sum(mix.values()) or 1
    lines.append("## exec-class mix")
    lines.append(
        ", ".join(
            f"{cls}={n} ({_fmt_pct(n / total)})"
            for cls, n in sorted(mix.items(), key=lambda kv: -kv[1])
        )
    )
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Compute the one-screen mine-sessions scoreboard.",
    )
    parser.add_argument("--events", type=Path, default=outdir.out_path("events.jsonl"))
    parser.add_argument("--out-md", type=Path, default=outdir.out_path("scoreboard.md"))
    parser.add_argument(
        "--out-json", type=Path, default=outdir.out_path("scoreboard.json")
    )
    args = parser.parse_args(argv)

    events = detect.load_events(args.events)
    events.sort(key=lambda e: (e.session, e.seq))

    sb = build_scoreboard(events)
    md = scrub(render_markdown(sb))

    args.out_md.parent.mkdir(parents=True, exist_ok=True)
    args.out_json.parent.mkdir(parents=True, exist_ok=True)
    args.out_md.write_text(md, encoding="utf-8")
    args.out_json.write_text(
        json.dumps(sb, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"stats: wrote {args.out_md} and {args.out_json}")


if __name__ == "__main__":
    main()
