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
* **Taproot claim hub** (Taproot slice A1, "living citation") → a
  ``[pub_id]`` that resolves to a ``TAPROOT:claim`` hub instead
  expands to its *current* derived ``establishes`` originator(s)
  (:func:`precis.taproot.seniority.derive_evidence`), falling back
  to corroborators when no originator is derived yet, and to
  in-flight when the hub has no supporting evidence at all. Because
  the split is recomputed on every run, a later-discovered
  originator or a claim merge improves the ``.bib`` output on the
  *next* ``resolve`` — the cite never needs hand-editing. Multiple
  originators render as a multi-key cite: ``\\cite{a,b}`` (LaTeX) /
  ``[a; b]`` (plain/markdown).

  **Authorial pin (Taproot slice A2).** The living default can be
  overridden inline: ``[<pub_id>>pa5,pc293]`` cites exactly those
  universal handles instead of the derived originators (**replace**);
  ``[<pub_id>+pa5]`` cites the derived originators *plus* those
  (**supplement**, deduped). A ``pc<id>`` (paper-chunk/passage) handle
  resolves to its parent paper's cite_key. Purely syntactic — no
  storage, no draft-side edge. When a **replace** pin diverges from the
  current derived ``establishes`` set, a stderr advisory fires
  (``resolve: [<pub_id>] pinned {...} but derived originator is {...}
  — reconsider``); ``--strict-pins`` turns that into a CI-gate exit 3.
  A **supplement** pin is purely additive and has no divergence concept
  — it never fires the advisory or trips ``--strict-pins``. A pin on a
  non-hub finding is meaningless and is ignored (with a warning); an
  unresolvable pinned handle is skipped (with a warning), and an empty
  ``>``-replace set falls through to the normal hub resolution rather
  than dropping the citation.

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
from precis.taproot.seniority import EvidenceEdge, HubEvidence, derive_evidence
from precis.utils import handle_registry
from precis.utils.pub_id_lookup import PLACEHOLDER_RE as _PLACEHOLDER_RE
from precis.utils.pub_id_lookup import lookup_pub_id_finding as _lookup_pub_id_finding
from precis.utils.pub_id_lookup import parse_pin as _parse_pin

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
        "--strict-verified",
        action="store_true",
        help="In addition to --strict, also treat *unverified* "
        "established findings as in-flight. Implies --strict. "
        "Use when a manuscript requires every cite chain to have "
        "been human-reviewed via ``precis verify``.",
    )
    p.add_argument(
        "--strict-pins",
        action="store_true",
        help="Exit code 3 if any authorial pin (Taproot slice A2, "
        "``[<pub_id>>...]`` / ``[<pub_id>+...]``) diverges from the "
        "current derived originator(s). Mirrors --strict-verified but "
        "for author overrides — use as a CI gate to catch a pin gone "
        "stale after the evidence graph moved on.",
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
            require_verified=args.strict_verified,
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
    # Pin divergence advisories (Taproot slice A2) — a distinct format
    # from the generic warnings above, always shown regardless of
    # --strict-pins (advisory by default; the flag only affects the
    # exit code).
    for message in summary.pin_divergences:
        print(f"resolve: {message}", file=sys.stderr)

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

    if (args.strict or args.strict_verified) and summary.inflight_pub_ids:
        flag = "--strict-verified" if args.strict_verified else "--strict"
        print(
            f"resolve: {flag}: {len(summary.inflight_pub_ids)} "
            "placeholder(s) still in flight; exiting 3",
            file=sys.stderr,
        )
        sys.exit(3)
    if args.strict_pins and summary.diverged_pub_ids:
        print(
            f"resolve: --strict-pins: {len(summary.diverged_pub_ids)} "
            "pin divergence(s); exiting 3",
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
        # Taproot slice A2 (authorial cite pinning): a pin diverging from
        # the current derived `establishes` set. `pin_divergences` holds
        # ready-to-print stderr lines (advisory, always shown);
        # `diverged_pub_ids` is what --strict-pins gates the exit code on.
        self.pin_divergences: list[str] = []
        self.diverged_pub_ids: list[str] = []


def _resolve_text(
    text: str,
    *,
    store: Store,
    format: str,
    ascii_mode: bool,
    keep_id: bool,
    require_verified: bool = False,
) -> tuple[str, _Summary]:
    """Walk placeholders left → right, substituting where possible.

    When ``require_verified`` is True, an established finding whose
    ``human_verified_at`` is still NULL is rendered as in-flight
    (not substituted) so the strict-verified gate refuses to ship.
    """
    summary = _Summary()
    lookups: dict[str, dict[str, Any] | None] = {}
    hub_evidence_cache: dict[str, HubEvidence] = {}
    hub_keys_cache: dict[str, tuple[list[str], list[tuple[str, str]]]] = {}

    def _lookup(pub_id: str) -> dict[str, Any] | None:
        if pub_id not in lookups:
            lookups[pub_id] = _lookup_finding(store, pub_id)
        return lookups[pub_id]

    def _evidence_for(pub_id: str, hub_ref_id: int) -> HubEvidence:
        if pub_id not in hub_evidence_cache:
            hub_evidence_cache[pub_id] = derive_evidence(store, hub_ref_id)
        return hub_evidence_cache[pub_id]

    def _lookup_hub_keys(
        pub_id: str, hub_ref_id: int
    ) -> tuple[list[str], list[tuple[str, str]]]:
        if pub_id not in hub_keys_cache:
            evidence = _evidence_for(pub_id, hub_ref_id)
            hub_keys_cache[pub_id] = _hub_evidence_cite_keys(store, evidence)
        return hub_keys_cache[pub_id]

    def _sub(match: re.Match[str]) -> str:
        # Taproot slice A2: decode the token (pub_id + optional pin) via
        # the one shared parser both `resolve` and the reference ring use,
        # rather than hand-rolling the group split here.
        parsed = _parse_pin(match.group(0))
        assert parsed is not None  # PLACEHOLDER_RE already matched this span
        pub_id, pin_op, pin_handles = parsed
        finding = _lookup(pub_id)
        if finding is None:
            # No finding with this pub_id (or it's a different kind).
            # Don't touch the text — almost certainly real prose
            # bracket content that happens to match the alphabet.
            return match.group(0)
        if finding["is_hub"]:
            # Living citation (Taproot slice A1): a claim hub never
            # resolves off its own STATUS tag or a stored
            # primary_cite_key — the seniority split is re-derived
            # from the evidence graph on every run, so a later-
            # discovered originator or a hub merge improves the
            # output next time this command runs, with no manual
            # re-cite needed.
            cite_keys, notes = _lookup_hub_keys(pub_id, finding["ref_id"])
            for note_status, detail in notes:
                summary.warnings.append((pub_id, note_status, detail))
            if pin_op is not None:
                # Authorial pin (Taproot slice A2) — override or extend
                # the living default, syntactically, no storage.
                cite_keys = _apply_pin(
                    store,
                    pub_id=pub_id,
                    op=pin_op,
                    handles=pin_handles,
                    derived_cite_keys=cite_keys,
                    evidence=_evidence_for(pub_id, finding["ref_id"]),
                    summary=summary,
                )
            if not cite_keys:
                summary.inflight_pub_ids.append(pub_id)
                summary.warnings.append(
                    (
                        pub_id,
                        "tracing",
                        "claim hub has no resolvable evidence yet — still tracing",
                    )
                )
                return _render_inflight(pub_id, format, ascii_mode)
            summary.resolved_count += 1
            return _render_established_multi(cite_keys, format)
        if pin_op is not None:
            # A pin on a non-hub finding is meaningless — the finding
            # already resolves off its own primary_cite_key, there's no
            # "derived originator" set to override. Warn and resolve
            # normally rather than erroring.
            summary.warnings.append(
                (
                    pub_id,
                    "pin-ignored",
                    "pin only applies to a Taproot claim hub cite — "
                    "ignored for a regular finding",
                )
            )
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
            if require_verified and not finding.get("human_verified"):
                # --strict-verified: established but no human review
                # → treat as in-flight so the gate refuses to ship.
                # The substitution doesn't happen and the placeholder
                # carries the in-flight marker.
                summary.inflight_pub_ids.append(pub_id)
                summary.warnings.append(
                    (
                        pub_id,
                        "unverified",
                        "established but not human-verified; "
                        f"run `precis verify {pub_id}` to clear",
                    )
                )
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

    Returns ``{ref_id, status, primary_cite_key, dead_reason,
    human_verified, is_hub}``. ``human_verified`` is a bool —
    ``--strict-verified`` reads it to decide whether to substitute.
    ``is_hub`` is True iff the finding carries ``TAPROOT:claim`` — a
    living-citation claim hub, resolved via
    :func:`_hub_evidence_cite_keys` instead of the status/
    primary_cite_key path below (Taproot slice A1).

    Thin wrapper over :func:`precis.utils.pub_id_lookup.lookup_pub_id_finding`
    — the shared lookup :mod:`precis.utils.refeye`'s Claims mining (Taproot
    slice R1) also uses, so the two surfaces agree on what a ``[pub_id]``
    resolves to. Kept here under this name for existing test imports.
    """
    return _lookup_pub_id_finding(store, pub_id)


def _cite_keys_for_group(
    store: Store, edges: list[EvidenceEdge]
) -> tuple[list[str], list[int]]:
    """Resolve each edge's paper to its (oldest) ``cite_key`` alias.

    Returns ``(cite_keys, skipped_ref_ids)`` — a paper with no
    ``cite_key`` alias at all is dropped from the render rather than
    failing the whole hub, and its ``ref_id`` is reported back so the
    caller can warn about it.
    """
    cite_keys: list[str] = []
    skipped: list[int] = []
    for edge in edges:
        aliases = store.ref_cite_keys(edge.paper_ref_id)
        if aliases:
            cite_keys.append(aliases[0])
        else:
            skipped.append(edge.paper_ref_id)
    return cite_keys, skipped


def _hub_evidence_cite_keys(
    store: Store, evidence: HubEvidence
) -> tuple[list[str], list[tuple[str, str]]]:
    """Locked resolution policy for a claim hub's living citation.

    1. Derived ``establishes`` originators, if any have a cite_key.
    2. Else ``corroborators``, if any have a cite_key (best-available
       fallback — the caller's warnings note these aren't derived
       originators yet).
    3. Else empty — the caller treats the hub as in-flight.

    Returns ``(cite_keys, notes)`` where ``notes`` are ``(status,
    detail)`` diagnostic pairs meant for ``_Summary.warnings``
    (skipped no-cite_key papers, the corroborator-fallback flag).
    """
    notes: list[tuple[str, str]] = []
    originator_keys, skipped = _cite_keys_for_group(store, evidence.originators)
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"originator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if originator_keys:
        return originator_keys, notes

    corroborator_keys, skipped = _cite_keys_for_group(store, evidence.corroborators)
    for ref_id in skipped:
        notes.append(
            (
                "established",
                f"corroborator paper ref_id={ref_id} has no cite_key — skipped",
            )
        )
    if corroborator_keys:
        notes.append(
            (
                "established",
                "resolved via corroborator(s) — no derived originator yet",
            )
        )
        return corroborator_keys, notes

    return [], notes


def _resolve_pin_handle(store: Store, handle: str) -> tuple[int, str] | None:
    """Resolve one authorial pin handle (Taproot slice A2) to
    ``(paper_ref_id, cite_key)``.

    A ``pc<id>`` (paper-chunk/passage) handle resolves to its **parent
    paper** — the ``.bib`` is paper-level, so pinning a passage means
    "grounded at this figure," not a separate citable unit.
    :func:`~precis.store.Store.resolve_handle` already does that
    parent-lookup for a chunk handle (``ResolvedHandle.ref_id`` is the
    owning ref), so this reuses it rather than hand-rolling chunk→paper
    resolution.

    ``None`` when the handle isn't well-formed, doesn't resolve to a
    live paper, or that paper has no ``cite_key`` alias — the caller
    warns and skips.
    """
    resolved = store.resolve_handle(handle)
    if resolved is None or resolved.kind != "paper":
        return None
    aliases = store.ref_cite_keys(resolved.ref_id)
    if not aliases:
        return None
    return resolved.ref_id, aliases[0]


def _apply_pin(
    store: Store,
    *,
    pub_id: str,
    op: str,
    handles: list[str],
    derived_cite_keys: list[str],
    evidence: HubEvidence,
    summary: _Summary,
) -> list[str]:
    """Apply an authorial pin (Taproot slice A2) to a hub's derived
    cite_keys — ``op`` is ``'>'`` (replace) or ``'+'`` (supplement).

    Resolves each pinned handle (:func:`_resolve_pin_handle`, deduped by
    paper ref_id, first-seen order), warning + skipping an unresolvable
    one. Records a divergence advisory on ``summary`` when the pinned
    paper set differs from the hub's *actually derived* ``establishes``
    originators (not the corroborator-fallback set — a pin only
    "diverges" from a real seniority split) — **replace (``>``) only**.
    A supplement (``+``) pin is purely additive ("derived plus these"),
    so its handle set legitimately differs from the full derived set on
    every normal use; it has no divergence concept and never fires the
    advisory or trips ``--strict-pins``.

    ``'>'`` (replace) with an empty resolved pin set falls back to
    ``derived_cite_keys`` unchanged, with a warning — a citation must
    never silently disappear because a pin went stale.
    """
    pinned: list[tuple[int, str]] = []
    seen_ref_ids: set[int] = set()
    for handle in handles:
        resolved = _resolve_pin_handle(store, handle)
        if resolved is None:
            summary.warnings.append(
                (
                    pub_id,
                    "pin",
                    f"pinned handle {handle} did not resolve to a cited "
                    "paper — skipped",
                )
            )
            continue
        ref_id, cite_key = resolved
        if ref_id in seen_ref_ids:
            continue
        seen_ref_ids.add(ref_id)
        pinned.append((ref_id, cite_key))

    pinned_ref_ids = {ref_id for ref_id, _ in pinned}
    pinned_keys = [cite_key for _, cite_key in pinned]

    if op == ">":
        # Divergence advisory — replace only (see docstring: a supplement
        # pin has no divergence concept).
        originator_ref_ids = {edge.paper_ref_id for edge in evidence.originators}
        if (
            pinned_ref_ids
            and originator_ref_ids
            and pinned_ref_ids != originator_ref_ids
        ):
            pinned_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r) for r in pinned_ref_ids
                )
            )
            derived_str = ", ".join(
                sorted(
                    handle_registry.format_handle("paper", r)
                    for r in originator_ref_ids
                )
            )
            summary.pin_divergences.append(
                f"[{pub_id}] pinned {{{pinned_str}}} but derived originator "
                f"is {{{derived_str}}} — reconsider"
            )
            summary.diverged_pub_ids.append(pub_id)

        if pinned_keys:
            return pinned_keys
        summary.warnings.append(
            (
                pub_id,
                "pin",
                "replace pin resolved to no usable cite_keys — falling "
                "back to derived hub resolution",
            )
        )
        return derived_cite_keys
    # op == "+": supplement — derived originators first, pinned appended,
    # deduped by cite_key, deterministic (pin order after derived order).
    return derived_cite_keys + [
        key for key in pinned_keys if key not in derived_cite_keys
    ]


def _render_established(primary_cite_key: str, format: str) -> str:
    return _render_established_multi([primary_cite_key], format)


def _render_established_multi(cite_keys: list[str], format: str) -> str:
    """Render one or more cite_keys — the multi-key case a Taproot
    claim hub with several derived originators needs.

    LaTeX: comma-joined into one ``\\cite{...}`` (biblatex multi-key).
    Plain/markdown: semicolon-space-joined inside one bracket pair.
    """
    if format == "latex":
        return f"\\cite{{{','.join(cite_keys)}}}"
    return f"[{'; '.join(cite_keys)}]"


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
