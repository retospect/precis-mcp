"""``precis resolve`` — substitute finding ``[pub_id]`` placeholders.

When an agent creates a finding via ``put(kind='finding', ...)`` it
gets back a 6-char ``pub_id`` (base32 lowercase). The agent drops
that pub_id in their draft document as ``[ab12c3]`` — a placeholder
the chase will eventually resolve to a real cite_key.

This command rewrites those placeholders at document-finalisation
time:

* **Established** finding → substitute the primary cite_key. Plain
  text gets ``[fischer13]``; LaTeX gets ``\\cite{fischer13}``.
* **In-flight** finding (``STATUS:tracing`` / ``:multi_candidate``)
  → leave the placeholder and emit a warning to stderr, unless
  ``--strict`` (then exit non-zero). In LaTeX, additionally emit
  a stub ``.bib`` entry so the document still compiles.
* **Dead-chain** finding → fail unless ``--keep-id``; with
  ``--keep-id`` the placeholder is annotated with the failure
  reason inline.

In-flight visibility markers (deliberately obvious during proof-
reading so authors don't ship placeholders by accident):

| Format | Established | In flight |
| --- | --- | --- |
| plain | ``[fischer13]`` | ``[ab12c3 ⏳]`` |
| markdown | ``[fischer13]`` | ``[ab12c3 ⏳]`` |
| LaTeX (default) | ``\\cite{fischer13}`` | ``\\cite{ab12c3}\\,\\textsuperscript{⏳}`` |
| LaTeX ``--ascii`` | ``\\cite{fischer13}`` | ``\\cite{ab12c3}\\,\\textsuperscript{*}`` |

Input: positional path argument, ``-`` for stdin, or ``--text=…``.
Output: stdout (or ``--inplace`` to rewrite the file).

``--strict`` is the right flag for CI gates on manuscripts: it
exits non-zero if any placeholder is still in flight, so the
build catches "you forgot to wait for the chase" before the PDF
goes out.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from precis.cli._common import resolve_dsn
from precis.store import Store

# Placeholder grammar: ``[<6 base32 lowercase chars>]``. The same
# alphabet :func:`precis.identity.make_pub_id` produces, so any pub
# id ever minted matches and bracketed strings of other shapes
# (cite keys, S2 ids, prose ALL-CAPS) don't.
_PLACEHOLDER_RE = re.compile(r"\[([a-z2-7]{6})\]")

# Render markers for in-flight findings. Unicode default; ASCII
# fallback via --ascii so LaTeX targets without xetex/luatex still
# build (and so terminals that can't render ⏳ remain readable).
_HOURGLASS = "⏳"
_ASCII_MARKER = "*"


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "resolve",
        help="Substitute finding [pub_id] placeholders with cite_keys.",
        description=(
            "Rewrite finding placeholders in a draft document. "
            "Established findings get the primary cite_key; "
            "in-flight findings stay placeholders (with a visible "
            "marker) until the chase establishes them. Use --strict "
            "as a CI gate so a manuscript never ships with an "
            "unresolved [pub_id]."
        ),
    )
    p.add_argument(
        "input",
        nargs="?",
        default="-",
        help="Path to a text file, or '-' for stdin (default).",
    )
    p.add_argument(
        "--text",
        default=None,
        help="Resolve a literal string from the CLI (instead of a file).",
    )
    p.add_argument(
        "--format",
        choices=("plain", "markdown", "latex"),
        default="plain",
        help="Output format. Default: plain. 'latex' rewrites to "
        r"``\cite{cite_key}`` and emits a stub .bib for in-flight "
        "findings on a sibling ``--bib`` file when requested.",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Exit code 3 if any placeholder is still in flight. "
        "Use as a CI gate on manuscripts.",
    )
    p.add_argument(
        "--keep-id",
        action="store_true",
        help="When a placeholder resolves but the finding is dead "
        "(STATUS:dead_chain / :cycle), keep the [pub_id] in the "
        "output with an annotation rather than failing.",
    )
    p.add_argument(
        "--ascii",
        action="store_true",
        help="Replace the unicode ⏳ in-flight marker with an ASCII "
        "asterisk. Useful for LaTeX targets that can't be coerced "
        "to xetex/luatex.",
    )
    p.add_argument(
        "--inplace",
        action="store_true",
        help="When INPUT is a file, rewrite it in place. Original is "
        "saved alongside with a ``.precis.bak`` suffix.",
    )
    p.add_argument(
        "--bib",
        default=None,
        help="LaTeX only: write stub ``@misc{pub_id, …}`` entries "
        "for every in-flight finding to this path so the document "
        "still compiles. Rerun precis resolve after the chase "
        "establishes them.",
    )
    p.add_argument(
        "--database-url",
        default=None,
        help="Override PRECIS_DATABASE_URL.",
    )


def run(args: argparse.Namespace) -> None:
    text, src_path = _read_input(args)
    dsn = resolve_dsn(args.database_url)
    store = Store.connect(dsn)
    try:
        resolved, summary = _resolve_text(
            text,
            store=store,
            format=args.format,
            ascii_mode=args.ascii,
            keep_id=args.keep_id,
        )
    finally:
        store.close()

    # Diagnostics — every non-established placeholder gets a line
    # on stderr so the operator sees what's pending without having
    # to grep the output for ⏳.
    for pub_id, status, detail in summary.warnings:
        print(
            f"resolve: [{pub_id}] {status}: {detail}",
            file=sys.stderr,
        )

    if args.bib and args.format == "latex" and summary.inflight_pub_ids:
        Path(args.bib).write_text(_emit_stub_bib(summary.inflight_pub_ids))
        print(
            f"resolve: wrote {len(summary.inflight_pub_ids)} stub bib "
            f"entries to {args.bib}",
            file=sys.stderr,
        )

    if args.inplace and src_path is not None:
        backup = src_path.with_suffix(src_path.suffix + ".precis.bak")
        backup.write_text(text)
        src_path.write_text(resolved)
        print(
            f"resolve: rewrote {src_path} (backup: {backup})",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(resolved)
        if not resolved.endswith("\n"):
            sys.stdout.write("\n")

    if args.strict and summary.inflight_pub_ids:
        print(
            f"resolve: --strict: {len(summary.inflight_pub_ids)} "
            "placeholder(s) still in flight; exiting 3",
            file=sys.stderr,
        )
        sys.exit(3)
    if summary.dead_pub_ids and not args.keep_id:
        print(
            f"resolve: {len(summary.dead_pub_ids)} dead-chain "
            "placeholder(s) — use --keep-id to render anyway",
            file=sys.stderr,
        )
        sys.exit(3)


# ── Internals ──────────────────────────────────────────────────────


def _read_input(args: argparse.Namespace) -> tuple[str, Path | None]:
    """Read the input text, returning ``(text, src_path_or_None)``.

    Path is non-None only when the input came from a file (for the
    --inplace path); stdin / --text return None to fail-loud on
    inplace misuse.
    """
    if args.text is not None:
        if args.inplace:
            print(
                "resolve: --inplace requires a file input, not --text",
                file=sys.stderr,
            )
            sys.exit(2)
        return args.text, None
    if args.input == "-":
        if args.inplace:
            print(
                "resolve: --inplace requires a file input, not stdin",
                file=sys.stderr,
            )
            sys.exit(2)
        return sys.stdin.read(), None
    src = Path(args.input)
    if not src.is_file():
        print(f"resolve: file not found: {src}", file=sys.stderr)
        sys.exit(2)
    return src.read_text(), src


class _Summary:
    """Accumulator for diagnostics shown after the rewrite."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, str, str]] = []  # (pub_id, status, detail)
        self.inflight_pub_ids: list[str] = []
        self.dead_pub_ids: list[str] = []
        self.resolved_count: int = 0


def _resolve_text(
    text: str,
    *,
    store: Store,
    format: str,
    ascii_mode: bool,
    keep_id: bool,
) -> tuple[str, _Summary]:
    """Walk placeholders left → right, substituting where possible."""
    summary = _Summary()
    lookups: dict[str, dict[str, Any] | None] = {}

    def _lookup(pub_id: str) -> dict[str, Any] | None:
        if pub_id not in lookups:
            lookups[pub_id] = _lookup_finding(store, pub_id)
        return lookups[pub_id]

    def _sub(match: re.Match[str]) -> str:
        pub_id = match.group(1)
        finding = _lookup(pub_id)
        if finding is None:
            # No finding with this pub_id (or it's a different kind).
            # Don't touch the text — almost certainly real prose
            # bracket content that happens to match the alphabet.
            return match.group(0)
        status = finding["status"] or "tracing"
        if status == "established":
            primary = finding.get("primary_cite_key")
            if not primary:
                # Defensive: established without a cite_key — treat
                # like in-flight so the document doesn't ship with a
                # missing reference.
                summary.warnings.append(
                    (pub_id, "established", "no primary_cite_key on meta")
                )
                summary.inflight_pub_ids.append(pub_id)
                return _render_inflight(pub_id, format, ascii_mode)
            summary.resolved_count += 1
            return _render_established(primary, format)
        if status in ("dead_chain", "cycle", "primary_deleted"):
            summary.dead_pub_ids.append(pub_id)
            summary.warnings.append(
                (
                    pub_id,
                    status,
                    finding.get("dead_reason") or "(no reason recorded)",
                )
            )
            if keep_id:
                return _render_dead(pub_id, format, status)
            # Leave the placeholder as-is; the main flow will exit 3
            # unless --keep-id was passed.
            return match.group(0)
        # In-flight (tracing / multi_candidate / etc.)
        summary.inflight_pub_ids.append(pub_id)
        summary.warnings.append(
            (pub_id, status, "still tracing — re-run after chase advances")
        )
        return _render_inflight(pub_id, format, ascii_mode)

    return _PLACEHOLDER_RE.sub(_sub, text), summary


def _lookup_finding(store: Store, pub_id: str) -> dict[str, Any] | None:
    """Resolve a pub_id to its finding ref, or None when there's no
    matching finding (different kind, no such row, soft-deleted).

    Returns ``{ref_id, status, primary_cite_key, dead_reason}``.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.deleted_at, r.meta,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id
                       AND t.namespace = 'STATUS'
                     LIMIT 1) AS status
              FROM ref_identifiers ri
              JOIN refs r ON r.ref_id = ri.ref_id
             WHERE ri.id_kind = 'pub_id' AND ri.id_value = %s
            """,
            (pub_id,),
        ).fetchone()
    if row is None:
        return None
    ref_id, kind, deleted_at, meta, status = row
    if kind != "finding":
        return None
    if deleted_at is not None:
        return None
    meta = dict(meta or {})
    return {
        "ref_id": int(ref_id),
        "status": status,
        "primary_cite_key": meta.get("primary_cite_key"),
        "dead_reason": meta.get("dead_reason"),
    }


def _render_established(primary_cite_key: str, format: str) -> str:
    if format == "latex":
        return f"\\cite{{{primary_cite_key}}}"
    return f"[{primary_cite_key}]"


def _render_inflight(pub_id: str, format: str, ascii_mode: bool) -> str:
    marker = _ASCII_MARKER if ascii_mode else _HOURGLASS
    if format == "latex":
        # \, is a thin-space; superscript keeps the marker visually
        # tight against the citation without colliding with prose.
        return f"\\cite{{{pub_id}}}\\,\\textsuperscript{{{marker}}}"
    return f"[{pub_id} {marker}]"


def _render_dead(pub_id: str, format: str, status: str) -> str:
    """Visible annotation for dead-chain placeholders kept via --keep-id."""
    tag = {"dead_chain": "dead", "cycle": "cycle", "primary_deleted": "gone"}.get(
        status, status
    )
    if format == "latex":
        return f"\\cite{{{pub_id}}}\\,\\textsuperscript{{[{tag}]}}"
    return f"[{pub_id} ✗{tag}]"


def _emit_stub_bib(pub_ids: list[str]) -> str:
    """Build a stub ``@misc{...}`` block for in-flight pub_ids.

    Keeps bibtex/biblatex happy so the document compiles even with
    unresolved placeholders. Each entry's title flags the in-flight
    state prominently so a careful proofreader spots the leak.
    """
    seen: set[str] = set()
    lines = [
        "% Auto-generated by `precis resolve`. Remove + rerun after",
        "% the chase establishes each finding.",
    ]
    for pid in pub_ids:
        if pid in seen:
            continue
        seen.add(pid)
        lines.append(
            f"@misc{{{pid},\n"
            f"  title = {{[in-flight finding {pid}]}},\n"
            "  note  = {Auto-stub by precis resolve; rerun after chase establishes.},\n"
            "}"
        )
    return "\n".join(lines) + "\n"


__all__ = ["add_parser", "run"]
