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
Static gates of ``docs/design/docs-and-skills-redesign.md``):

- Frontmatter parses; ``flavor:`` is one of the four defined values.
- Every ``{{include …}}`` directive resolves.
- No chunk's body exceeds the chunk-size budget (decision 5).
- For ``FLAVOR:runbook`` skills: every ``invokes_personas:`` entry
  resolves to an existing ``FLAVOR:persona`` skill in the same scan.

A file failing any gate goes into :class:`IngestFailure`; the scan
continues so one bad skill doesn't block the rest.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

from precis.handlers._skill_common import (
    FrontmatterError,
    SkillFrontmatter,
    flavor_tag,
    parse_frontmatter,
)
from precis.ingest.skill_template import IncludeError, Includer
from precis.skill_index.chunker import Chunk, chunk_by_h2

log = logging.getLogger(__name__)


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
            failures.append(
                IngestFailure(slug=slug, file_path=path, reason=str(exc))
            )
            continue
        plans.append(plan)

    plans, failures = _validate_cross_references(plans, failures)
    return ScanResult(plans=tuple(plans), failures=tuple(failures))


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

    for c in chunks:
        if len(c.text) > chunk_budget_chars:
            label = c.heading or "(head)"
            raise _PlanError(
                f"chunk {label!r} body exceeds the chunk-size budget "
                f"({len(c.text)} > {chunk_budget_chars} chars). "
                f"Split the section into multiple H2s (decision 5)."
            )

    tags = _build_tags(fm)

    return IngestPlan(
        slug=slug,
        file_path=path,
        file_sha256=file_sha256,
        frontmatter=fm,
        chunks=tuple(chunks),
        tags=tags,
        expanded_text=expanded,
    )


def _build_tags(fm: SkillFrontmatter) -> tuple[str, ...]:
    """Emit the tag set declared by frontmatter (decisions 7 + 13)."""
    out: list[str] = []
    ft = flavor_tag(fm)
    if ft is not None:
        out.append(ft)
    if fm.available_when:
        # Lowercase prefix → accumulates, so a skill could declare
        # multiple required env vars in the future without breaking
        # the existing tag-replace semantics.
        out.append(f"requires:{fm.available_when}")
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
                parts.append(
                    f"slugs are not FLAVOR:persona: {wrong_flavor}"
                )
            failures.append(IngestFailure(
                slug=plan.slug,
                file_path=plan.file_path,
                reason="invokes_personas validation failed — " + "; ".join(parts),
            ))
        else:
            good.append(plan)
    return good, failures
