#!/usr/bin/env python3
"""Render `detect.py` candidates (and an unbiased random arm) to small,
scrubbed evidence cards a `forensics` agent can read without ever touching a
raw transcript.

Each candidate becomes ``out/cards/<detector>/<nn>-<slug>.md``: a header
(detector, signature, counts, date range, the detector's own "what this
means" line) followed by one ≤40-line block per evidence occurrence — the
call, its args, the error/result head, and ±2 surrounding turns of context.

``--random N`` writes a second, detector-free arm: N uniformly-sampled ~8
event windows from random sessions. The catalogue in ``detect.py`` can only
ever confirm shapes someone already thought to encode; this arm is how a
pass finds the ones nobody did.

Every byte written here passes through ``redact.scrub`` — signatures and
metrics came out of ``detect.py`` already scrubbed once, but event payloads
(``arg_digest``/``err_head``/``result_head``/``session``) are scrubbed again
here since this is the stage that actually re-quotes them into a file a
subagent reads and a human might paste elsewhere. Scrub is idempotent
(`redact.py`), so a double pass costs nothing and closes the gap if a
payload field was ever missed upstream.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import detect
import outdir
from redact import scrub
from schema import Event

CARD_BYTE_CAP = 20_000
BLOCK_LINE_CAP = 40
CONTEXT_TURNS = 2
RANDOM_WINDOW = 8


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "x")[:60]


def _summary_line(doc: str | None) -> str:
    if not doc:
        return ""
    para = doc.strip().split("\n\n", 1)[0]
    return " ".join(para.split())


def _format_event(ev: Event) -> list[str]:
    who = ev.tool or "assistant"
    head = f"[{ev.seq}] {who}"
    if ev.verb:
        head += f" verb={ev.verb} kind={ev.kind}"
    lines = [head]
    if ev.arg_keys:
        # Fall back to the key NAMES when there is no digest. Ledger events
        # carry `arg_keys` but never `arg_digest` (migration 0133 records
        # argument names only, never values), so rendering the digest alone
        # printed a bare `args:` with nothing after it — a card that looks
        # like the agent passed no arguments, when in fact the corpus just
        # cannot show their values.
        digest = scrub(ev.arg_digest)[:200] if ev.arg_digest else ""
        lines.append(f"    args: {digest or '(names only) ' + ', '.join(ev.arg_keys)}")
    if ev.is_error:
        lines.append(f"    ERROR ({ev.err_type}): {scrub(ev.err_head)[:200]}")
    elif ev.result_head:
        lines.append(f"    result ({ev.result_bytes}B): {scrub(ev.result_head)[:200]}")
    return lines


def _render_occurrence(
    ref: dict[str, Any], sessions: dict[str, list[Event]]
) -> list[str]:
    session = ref["session"]
    seq = ref["seq"]
    sess_events = sessions.get(session, [])
    idx = next((i for i, e in enumerate(sess_events) if e.seq == seq), None)
    lines = [f"### session {scrub(session)} seq {seq}"]
    if idx is None:
        lines.append("(event not found in this event set)")
        return lines
    lo = max(0, idx - CONTEXT_TURNS)
    hi = min(len(sess_events), idx + CONTEXT_TURNS + 1)
    for e in sess_events[lo:hi]:
        marker = ">>" if e.seq == seq else "  "
        for line in _format_event(e):
            lines.append(f"{marker} {line}")
    if len(lines) > BLOCK_LINE_CAP:
        lines = lines[: BLOCK_LINE_CAP - 1] + ["[... truncated]"]
    return lines


def _render_card(
    cand: dict[str, Any], sessions: dict[str, list[Event]], doc_summary: str
) -> str:
    header = [
        f"# {cand['detector']} — {cand['signature']}",
        "",
        f"n={cand['n']}  n_sessions={cand['n_sessions']}  "
        f"range={cand.get('first_ts') or '?'}..{cand.get('last_ts') or '?'}",
    ]
    if cand.get("metrics"):
        header.append(
            "metrics: " + ", ".join(f"{k}={v}" for k, v in cand["metrics"].items())
        )
    if cand.get("caveat"):
        header.append(f"caveat: {cand['caveat']}")
    header += ["", doc_summary, ""]

    body: list[str] = []
    for ref in cand["evidence"]:
        body.extend(_render_occurrence(ref, sessions))
        body.append("")

    text = "\n".join(header + body)
    text = scrub(text)
    encoded = text.encode("utf-8")
    if len(encoded) > CARD_BYTE_CAP:
        text = encoded[:CARD_BYTE_CAP].decode("utf-8", "ignore") + "\n[... truncated]\n"
    return text


def _random_cards(
    events: Sequence[Event], n: int, seed: int, out_dir: Path
) -> list[tuple[str, str]]:
    """Return [(filename, session)] written, for the index."""
    if n <= 0:
        return []
    rng = random.Random(seed)
    sessions = detect.by_session(events)
    eligible = sorted(s for s, evs in sessions.items() if len(evs) >= 3)
    if not eligible:
        return []
    picks = rng.sample(eligible, min(n, len(eligible)))
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for i, session in enumerate(picks, 1):
        sess_events = sessions[session]
        window = min(RANDOM_WINDOW, len(sess_events))
        start = rng.randint(0, len(sess_events) - window)
        chunk = sess_events[start : start + window]
        lines = [
            "# random sample (unbiased arm)",
            "",
            "No detector, no signature attached. The detector catalogue only "
            "ever confirms shapes we already thought to encode; this window "
            "is how a pass finds the ones we did not. Read it and ask: what "
            "confused this agent?",
            "",
            f"session {scrub(session)}  seq {chunk[0].seq}..{chunk[-1].seq}",
            "",
        ]
        for e in chunk:
            lines.extend(_format_event(e))
        text = scrub("\n".join(lines))
        fname = f"{i:02d}-{_slug(session)[:16]}.md"
        (out_dir / fname).write_text(text, encoding="utf-8")
        written.append((fname, session))
    return written


def _write_index(
    out_dir: Path,
    entries: list[tuple[str, str, str, int]],
    random_written: list[tuple[str, str]],
) -> None:
    lines = ["# cards index", ""]
    by_det: dict[str, list[str]] = defaultdict(list)
    for det, fname, sig, n in entries:
        by_det[det].append(f"- `{fname}` — n={n} — {sig}")
    for det in sorted(by_det):
        lines.append(f"## {det}")
        lines.extend(by_det[det])
        lines.append("")
    if random_written:
        lines.append("## random")
        for fname, session in random_written:
            lines.append(f"- `random/{fname}` — session {scrub(session)}")
        lines.append("")
    (out_dir / "INDEX.md").write_text(scrub("\n".join(lines)), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Render detect.py candidates to scrubbed, size-capped evidence cards.",
    )
    parser.add_argument("--events", type=Path, default=outdir.out_path("events.jsonl"))
    parser.add_argument(
        "--candidates", type=Path, default=outdir.out_path("candidates.json")
    )
    parser.add_argument("--out", type=Path, default=outdir.out_path("cards"))
    parser.add_argument(
        "--top", type=int, default=5, help="top N candidates per detector"
    )
    parser.add_argument(
        "--random", type=int, default=10, help="N random unbiased-arm windows"
    )
    parser.add_argument(
        "--seed", type=int, default=0, help="random-sample seed (deterministic)"
    )
    args = parser.parse_args(argv)

    events = detect.load_events(args.events)
    events.sort(key=lambda e: (e.session, e.seq))
    sessions = detect.by_session(events)

    candidates: list[dict[str, Any]] = json.loads(
        args.candidates.read_text(encoding="utf-8")
    )
    by_detector: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_detector[c["detector"]].append(c)

    args.out.mkdir(parents=True, exist_ok=True)
    index_entries: list[tuple[str, str, str, int]] = []
    for det, cands in by_detector.items():
        top_cands = sorted(cands, key=lambda c: c["n"], reverse=True)[: args.top]
        det_dir = args.out / det
        det_dir.mkdir(parents=True, exist_ok=True)
        fn = detect.DETECTORS.get(det)
        doc_summary = _summary_line(fn.__doc__ if fn else None)
        for i, cand in enumerate(top_cands, 1):
            # Slug from the SCRUBBED signature — a raw secret must never land
            # in a filename either, even though out/ is gitignored.
            fname = f"{i:02d}-{_slug(scrub(cand['signature']))}.md"
            text = _render_card(cand, sessions, doc_summary)
            (det_dir / fname).write_text(text, encoding="utf-8")
            index_entries.append(
                (det, f"{det}/{fname}", scrub(cand["signature"]), cand["n"])
            )

    random_written = _random_cards(events, args.random, args.seed, args.out / "random")
    _write_index(args.out, index_entries, random_written)

    print(
        f"cards: wrote {len(index_entries)} cards across {len(by_detector)} detectors "
        f"+ {len(random_written)} random windows to {args.out}"
    )


if __name__ == "__main__":
    main()
