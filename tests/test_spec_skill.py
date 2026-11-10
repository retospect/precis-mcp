"""Content contract for the precis-spec-help skill (gr568183).

The spec-in-memory format lived in memory nodes me568129 + six part-of
children (me568162..me568167) plus the transfer writer runbook me568489.
gr568183 promotes it to a runtime skill. This test pins the promotion:
the skill exists, declares itself correctly, and carries every node the
gripe names — so a later edit cannot silently drop a section again.
"""

from __future__ import annotations

from precis.utils.skill_common import parse_frontmatter
from tests._skill_roots import BUILTIN_SKILLS_DIR

SKILL_PATH = BUILTIN_SKILLS_DIR / "precis-spec-help.md"

#: Memory/gripe handles the skill must name — one per source node or
#: tracked defect the format sections depend on.
_REQUIRED_HANDLES = (
    "me568215",  # Specifications hub (me568162)
    "me568168",  # the ruling the format implements
    "me568172",  # first single-spec-variant feature node (me568165)
    "gr568171",  # file-keyed tooling prerequisite (me568168)
    "gr569350",  # deletion blocked → retire-pending pointer nodes
    "gr569538",  # provenance-vs-pin caveat for BUILT verdicts
    "gr621795",  # tags-plus-under search leak caveat (me568489)
)

#: One verbatim marker per required section topic (gr568183's ask list).
_REQUIRED_MARKERS = (
    "Spec: <slug>: <purpose>",  # spec root title form (me568163)
    "3500",  # section size guidance (me568164, me568167)
    "Feature: <name>",  # feature node title (me568165)
    "plan-of",  # one plan root todo per spec (me568166)
    "blocked-by",  # slice sequencing (me568166)
    "40 direct members",  # sub-hub size rule (me568167)
    "llm_tier",  # parented put guard (me568166)
    "supersedes",  # retirement edge (me568166, me568168)
    "fisheye",  # how a spec is read
    "slicer",  # consumers: slicer / ready agent / builder
    "ready agent",
    "builder",
    "landing commit",  # file deletion ships with the graph spec
)


def test_spec_skill_declares_itself() -> None:
    fm = parse_frontmatter(SKILL_PATH.read_text(encoding="utf-8"))
    assert fm.id == "precis-spec-help"
    assert fm.family == "work"
    assert fm.status == "active"
    assert "memory" in (fm.kinds or ())
    assert "todo" in (fm.kinds or ())


def test_spec_skill_carries_every_source_handle() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    missing = [h for h in _REQUIRED_HANDLES if h not in text]
    assert not missing, f"precis-spec-help lost source handles: {missing}"


def test_spec_skill_covers_every_asked_section() -> None:
    text = SKILL_PATH.read_text(encoding="utf-8")
    missing = [m for m in _REQUIRED_MARKERS if m not in text]
    assert not missing, f"precis-spec-help lost section markers: {missing}"


def test_spec_skill_h2_chunks_fit_the_ingest_budget() -> None:
    from precis.ingest.skill_ingest import DEFAULT_CHUNK_BUDGET_CHARS
    from precis.skill_index.chunker import _strip_front_matter, chunk_by_h2

    body = _strip_front_matter(SKILL_PATH.read_text(encoding="utf-8"))
    oversized = [
        (i, len(c.text))
        for i, c in enumerate(chunk_by_h2(body))
        if len(c.text) > DEFAULT_CHUNK_BUDGET_CHARS
    ]
    assert not oversized, f"H2 chunks over budget: {oversized}"
