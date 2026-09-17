"""Scan + plan stage for skill ingest.

Walks a directory of skill markdown files (recursively — subdirs
like ``data/skills/personas/`` are organisational only). For each
file, produces an :class:`IngestPlan` describing what the DB-write
stage will do: which chunks to insert, what tags to emit on the
ref, the ``file_sha256`` for the change-detection cache.

Pure — no DB access here. The DB-write stage (compare-by-hash,
advisory-lock claim, transactional swap) lives separately so this
planning phase stays unit-testable against the filesystem alone.

Static gates enforced at scan time (decision 5 / Quality gates §
Static gates of ``docs/backlog/docs-and-skills-redesign.md``):

- Frontmatter parses; ``flavor:`` is one of the four defined values.
- Every ``{{include …}}`` directive resolves.
- No chunk's body exceeds the chunk-size budget (decision 5).
- For ``FLAVOR:runbook`` skills: every ``invokes_personas:`` entry
  resolves to an existing ``FLAVOR:persona`` skill in the same scan.

Graph gates (docs/backlog/skill-graph.md slice 1) join the list above:

- Every ``[[slug]]`` wikilink resolves to a real slug in the same scan
  or a synthesised meta-skill (:data:`~precis.handlers._skill_common.
  SYNTH_SKILL_SLUGS` — ``precis-help``/``precis-status``/``precis-toc``/
  ``toc``, not files) (no dangling link).
- Every ``tags:`` entry is a known tag (:data:`VALID_TAGS`) and not a
  registered kind name.
- ``kinds:`` is not present-but-empty (``kinds:`` with zero items).

These three are **hard-fail** — see :data:`GRAPH_GATES_HARD_FAIL` — as of
slice 2's last step, once the 160-file sweep populated ``tags:``/
``kinds:`` everywhere; a finding moves the plan into
:attr:`ScanResult.failures` instead of shipping it. (Slice 1 shipped
these in WARN mode while the sweep was in flight.) A singleton tag (used
by exactly one skill corpus-wide) is a lint, not a gate — always a
warning, never flipped to hard-fail.

A file failing any gate goes into :class:`IngestFailure`; the scan
continues so one bad skill doesn't block the rest.
"""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from precis.handlers._skill_common import (
    SYNTH_SKILL_SLUGS,
    FrontmatterError,
    SkillFrontmatter,
    extract_wikilinks,
    flavor_tag,
    parse_frontmatter,
    unknown_kinds,
    unknown_tags,
)
from precis.ingest.skill_template import IncludeError, Includer
from precis.skill_index.chunker import Chunk, chunk_by_h2

log = logging.getLogger(__name__)

#: Whether the three graph gates below are hard-fail (move the plan to
#: ``ScanResult.failures``) or WARN-mode (log + report via
#: ``ScanResult.warnings``, plan still ships). Slice 1 shipped WARN
#: (docs/backlog/skill-graph.md); slice 2's last step flips this now that
#: the 160-file sweep has populated ``tags:``/``kinds:`` everywhere —
#: ``tests/test_skill_ingest.py`` pins the real shipped corpus at zero
#: findings.
GRAPH_GATES_HARD_FAIL = True


#: Default chunk-body size budget in characters. bge-m3 handles up
#: to 8192 tokens; ~4000 chars ≈ 1000 tokens leaves headroom and
#: keeps each section focused. Authors who hit this should split
#: the section into multiple H2s (decision 5).
DEFAULT_CHUNK_BUDGET_CHARS = 4000


@dataclass(frozen=True)
class IngestPlan:
    """Resolved-and-validated content for one skill ready to ingest.

    Pure data — the DB-write stage consumes this and runs the
    advisory-lock claim + transactional swap (decision 11).
    """

    slug: str
    file_path: Path
    file_sha256: str
    frontmatter: SkillFrontmatter
    chunks: tuple[Chunk, ...]
    tags: tuple[str, ...]
    expanded_text: str

    #: ``[[slug]]`` wikilink targets found in the body, order-preserving,
    #: deduplicated, self-links dropped (docs/backlog/skill-graph.md
    #: slice 1). May contain a dangling target — see
    #: :func:`_validate_graph_gates`; the graph build
    #: (:mod:`precis.skill_index.graph`) re-derives the same edges from
    #: the raw text independently, so this field exists for the ingest
    #: gate, not as the graph's only source.
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngestFailure:
    """One reason a skill failed scan-time validation.

    The DB-write stage skips failed slugs; the previous version (if
    any) stays live. Failures are surfaced to operators / CI; the
    LLM gates also turn into failures of this shape.
    """

    slug: str
    file_path: Path
    reason: str

    def __str__(self) -> str:
        return f"[{self.slug}] {self.reason} ({self.file_path})"


@dataclass(frozen=True)
class ScanResult:
    """Outcome of :func:`scan_skill_dir`."""

    plans: tuple[IngestPlan, ...]
    failures: tuple[IngestFailure, ...]

    #: Non-fatal findings — WARN-mode graph gates (dangling link /
    #: unknown tag / empty ``kinds:``) and the always-warn singleton-tag
    #: lint. Each entry is pre-formatted (``str(IngestFailure(...))``
    #: shape) so a caller can log or report it without re-deriving the
    #: message. Empty when nothing to flag.
    warnings: tuple[str, ...] = ()


class _PlanError(ValueError):
    """Internal: raised by :func:`_plan_one` to convert into
    :class:`IngestFailure` at the outer loop."""


def scan_skill_dir(
    root: Path,
    *,
    includer: Includer | None = None,
    chunk_budget_chars: int = DEFAULT_CHUNK_BUDGET_CHARS,
) -> ScanResult:
    """Walk ``root`` recursively for ``*.md`` files; return plans + failures.

    The scan is deterministic — files are processed in lexicographic
    order so failure messages are stable across runs.
    """
    if not root.is_dir():
        raise FileNotFoundError(f"skill scan root does not exist: {root}")

    plans: list[IngestPlan] = []
    failures: list[IngestFailure] = []

    for path in sorted(root.rglob("*.md")):
        slug = path.stem
        try:
            plan = _plan_one(path, slug, includer, chunk_budget_chars)
        except _PlanError as exc:
            failures.append(IngestFailure(slug=slug, file_path=path, reason=str(exc)))
            continue
        plans.append(plan)

    plans, failures = _validate_cross_references(plans, failures)
    # Wikilinks resolve against every *scanned* slug, including ones a
    # prior gate failed: a broken file still exists as a link target, and
    # one bad file must not cascade an unrelated [[link]] into "dangling".
    corpus_slugs = {p.slug for p in plans} | {f.slug for f in failures}
    plans, failures, graph_warnings = _validate_graph_gates(
        plans, failures, corpus_slugs
    )
    warnings = graph_warnings + _singleton_tag_warnings(plans)
    for w in warnings:
        log.warning("skill graph gate: %s", w)
    return ScanResult(
        plans=tuple(plans), failures=tuple(failures), warnings=tuple(warnings)
    )


def _plan_one(
    path: Path,
    slug: str,
    includer: Includer | None,
    chunk_budget_chars: int,
) -> IngestPlan:
    text = path.read_text(encoding="utf-8")

    # Frontmatter — bubbles flavour validation up as a hard fail.
    try:
        fm = parse_frontmatter(text)
    except FrontmatterError as exc:
        raise _PlanError(f"frontmatter: {exc}") from exc

    # Template includes — single-pass expansion at ingest.
    expanded = text
    if includer is not None and "{{include" in text:
        try:
            expanded = includer.expand(text)
        except IncludeError as exc:
            raise _PlanError(f"include: {exc}") from exc

    # Hash the *post-expansion* text so upstream changes to
    # ``precis-common`` or any other included source propagate
    # the change-detection cache invalidation (decision 11).
    file_sha256 = hashlib.sha256(expanded.encode("utf-8")).hexdigest()

    chunks = chunk_by_h2(expanded)
    if not chunks:
        raise _PlanError(
            "no chunks produced — file is empty or every section is "
            "an alias group at EOF without a body."
        )

    oversized = [c for c in chunks if len(c.text) > chunk_budget_chars]
    if oversized:
        # Report every oversized section in one pass (gr344818) — a file
        # with several over-budget H2s used to surface them one re-run at
        # a time, since the loop raised on the first hit.
        details = "; ".join(
            f"chunk {(c.heading or '(head)')!r} body exceeds the "
            f"chunk-size budget ({len(c.text)} > {chunk_budget_chars} chars)"
            for c in oversized
        )
        raise _PlanError(
            f"{details}. Split the section(s) into multiple H2s (decision 5)."
        )

    tags = _build_tags(fm)
    links = tuple(t for t in extract_wikilinks(expanded) if t != slug)

    return IngestPlan(
        slug=slug,
        file_path=path,
        file_sha256=file_sha256,
        frontmatter=fm,
        chunks=tuple(chunks),
        tags=tags,
        expanded_text=expanded,
        links=links,
    )


def _build_tags(fm: SkillFrontmatter) -> tuple[str, ...]:
    """Emit the tag set declared by frontmatter (decisions 7 + 13, plus
    the graph axes' DB-side parity — docs/backlog/skill-graph.md
    slice 1, item 6: ``KIND:`` + topic tags alongside ``FLAVOR:``)."""
    out: list[str] = []
    ft = flavor_tag(fm)
    if ft is not None:
        out.append(ft)
    if fm.available_when:
        # Lowercase prefix → accumulates, so a skill could declare
        # multiple required env vars in the future without breaking
        # the existing tag-replace semantics.
        out.append(f"requires:{fm.available_when}")
    for kind in fm.kinds or ():
        # One ``KIND:`` tag per declared kind. Unlike ``FLAVOR:`` (a
        # single-valued discriminator, decision 7's "replaces within
        # prefix"), ``kinds:`` is multi-valued by design — a skill can
        # legitimately cover more than one kind. No DB write path
        # consumes these yet (slice 1 is scan-only parity, per
        # skill_ingest's module docstring); a future write stage
        # funnelling through the closed-vocab tag enforcer will need
        # ``KIND:`` registered there as multi-valued too.
        out.append(f"KIND:{kind}")
    for tag in fm.tags:
        # Lowercase prefix → accumulates, same rationale as ``requires:``.
        out.append(f"topic:{tag}")
    return tuple(out)


def _validate_cross_references(
    plans: list[IngestPlan],
    failures: list[IngestFailure],
) -> tuple[list[IngestPlan], list[IngestFailure]]:
    """Static gate: every ``invokes_personas:`` entry on a runbook
    resolves to a persona in the same scan."""
    slug_to_plan = {p.slug: p for p in plans}
    good: list[IngestPlan] = []
    for plan in plans:
        fm = plan.frontmatter
        if fm.flavor != "runbook" or not fm.invokes_personas:
            good.append(plan)
            continue
        missing: list[str] = []
        wrong_flavor: list[str] = []
        for ref in fm.invokes_personas:
            target = slug_to_plan.get(ref)
            if target is None:
                missing.append(ref)
            elif target.frontmatter.flavor != "persona":
                wrong_flavor.append(ref)
        if missing or wrong_flavor:
            parts: list[str] = []
            if missing:
                parts.append(f"missing persona slugs: {missing}")
            if wrong_flavor:
                parts.append(f"slugs are not FLAVOR:persona: {wrong_flavor}")
            failures.append(
                IngestFailure(
                    slug=plan.slug,
                    file_path=plan.file_path,
                    reason="invokes_personas validation failed — " + "; ".join(parts),
                )
            )
        else:
            good.append(plan)
    return good, failures


def _validate_graph_gates(
    plans: list[IngestPlan],
    failures: list[IngestFailure],
    corpus_slugs: set[str],
) -> tuple[list[IngestPlan], list[IngestFailure], list[str]]:
    """Graph gates (docs/backlog/skill-graph.md slice 1): dangling
    ``[[slug]]`` links, unknown/kind-named tags, invalid ``kinds:``.

    Hard-fail mode (:data:`GRAPH_GATES_HARD_FAIL` is ``True``, flipped at
    the end of slice 2): a finding moves the plan into ``failures``, same
    shape as :func:`_validate_cross_references`. WARN mode (slice 1,
    while the 160-file sweep was in flight): every finding becomes a
    warning string instead and the plan still ships — a caller can still
    exercise this path via ``monkeypatch.setattr(..., "GRAPH_GATES_HARD_FAIL",
    False)``, see ``tests/test_skill_ingest.py``.
    """
    # A wikilink resolves against every scanned corpus slug (passed in by
    # the caller so a plan a *prior* gate failed still counts — one bad
    # file must not cascade an unrelated ``[[link]]`` into "dangling")
    # *or* a synthesised meta-skill (``precis-help``/``precis-status``/
    # ``precis-toc``/``toc`` — not files, so never in ``plans``, but
    # legitimate `get(kind='skill', id=...)` targets all the same).
    slugs = corpus_slugs | SYNTH_SKILL_SLUGS
    good: list[IngestPlan] = []
    warnings: list[str] = []
    for plan in plans:
        findings: list[str] = []

        dangling = [t for t in plan.links if t not in slugs]
        if dangling:
            findings.append(f"dangling [[slug]] link(s): {dangling}")

        bad_tags = unknown_tags(plan.frontmatter.tags)
        if bad_tags:
            findings.append(
                f"unknown or kind-named tag(s): {bad_tags} "
                "(see VALID_TAGS in handlers/_skill_common.py)"
            )

        kinds = plan.frontmatter.kinds
        if kinds is not None:
            if kinds == ():
                findings.append(
                    "kinds: is present but empty — declare the kind(s) "
                    "this skill's recipes operate on, or omit the key "
                    "entirely (not-yet-migrated skills fall back to the "
                    "applies-to: inference)"
                )
            else:
                bad_kinds = unknown_kinds(kinds)
                if bad_kinds:
                    findings.append(
                        f"kinds: unknown kind(s) {bad_kinds} — not in "
                        "the kind registry (utils/handle_registry.py)"
                    )

        if not findings:
            good.append(plan)
            continue

        reason = "graph gate — " + "; ".join(findings)
        failure = IngestFailure(slug=plan.slug, file_path=plan.file_path, reason=reason)
        if GRAPH_GATES_HARD_FAIL:
            failures.append(failure)
        else:
            warnings.append(str(failure))
            good.append(plan)

    return good, failures, warnings


def _singleton_tag_warnings(plans: list[IngestPlan]) -> list[str]:
    """Lint (never a gate, always a warning): a tag used by exactly one
    skill corpus-wide is probably a typo or premature — nothing else
    will ever surface next to it via the ``tag=`` toc filter."""
    counts: Counter[str] = Counter()
    owner: dict[str, str] = {}
    for plan in plans:
        for tag in set(plan.frontmatter.tags):
            counts[tag] += 1
            owner[tag] = plan.slug
    slug_to_plan = {p.slug: p for p in plans}

    out: list[str] = []
    for tag in sorted(t for t, n in counts.items() if n == 1):
        slug = owner[tag]
        plan = slug_to_plan[slug]
        out.append(
            str(
                IngestFailure(
                    slug=slug,
                    file_path=plan.file_path,
                    reason=(
                        f"lint: tag {tag!r} is used by only this one "
                        "skill in the corpus — check for a typo, or tag "
                        "more related skills the same way"
                    ),
                )
            )
        )
    return out
