"""Load the memory writer's canonical reconciliation policy for garden judges.

The packaged skill section is the source of truth. Reusing the skill include
resolver keeps interactive guidance and machine prompts identical. The
prompt-cache version is a hand-bumped constant, not a digest of this prose:
a wording-only edit must not re-ask decided questions across all memory
detectors (gr732279).

The evaluator's memory-body checklist follows the same DRY rule (gr560762):
the per-type checks, the disagreement shapes and the standard of done with
its never-list are pinned verbatim from ``precis-fix-memory-gripe-help``, so
the hand repair path and the machine evaluator read one source. A missing
file or heading raises — the evaluator fails loudly rather than judging
checklist-less.

The detector set stays explicit to avoid a proposer → memory_reconcile →
proposer initialization cycle. Regression coverage follows MEMORY_ACTIONS so
new memory detectors cannot silently omit the policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Any

from precis.ingest.skill_template import DocResolver

_SKILL = "precis-memory-help"
_SECTION = "memory-reconciliation-policy"

#: Prompt-cache identity stamped on memory proposer rejection rows. Bump
#: by hand ONLY when the pinned policy's meaning for the proposer judge
#: changes — never a digest of the prose, so wording-only edits to the
#: skill sections leave decided questions closed (gr732279). Tests pin the
#: prose digest and fail when it drifts without this constant moving.
MEMORY_POLICY_VERSION = "garden-propose.memory.v5"

#: The checklist skill and the H2 sections the evaluator pins verbatim
#: (gr560762): the per-type checks, the disagreement shapes, the standard
#: of done with its never-list.
_CHECKLIST_SKILL = "precis-fix-memory-gripe-help"
_CHECKLIST_SECTIONS = (
    "the-memory-body-checklist",
    "choose-the-disagreement-shape",
    "rewrite-to-the-standard-of-done",
)
_MEMORY_DETECTORS = frozenset(
    {
        "anchor_rot",
        "compact",
        "dangling_mention",
        "entity_claim_stale",
        "grab_bag",
        "griped",
        "host_facts",
        "negative_undated",
        "notation",
        "restated_claim",
        "review_due",
        "source_drift",
        "thread_shape",
        "title_shape",
    }
)


def _skill_body(skill: str) -> str:
    return (
        resources.files("precis.data")
        .joinpath("skills", f"{skill}.md")
        .read_text(encoding="utf-8")
    )


@cache
def memory_reconciliation_policy() -> str:
    """The exact packaged H2 section, using the skill include grammar."""
    return DocResolver(docs={_SKILL: _skill_body(_SKILL)})(_SKILL, _SECTION)


@cache
def memory_body_checklist() -> str:
    """The pinned checklist sections, joined verbatim.

    Raises :class:`~precis.ingest.skill_template.IncludeError` when the
    skill file or a pinned heading is missing — by design: a prompt built
    without the checklist is worse than a failed call.
    """
    resolver = DocResolver(docs={_CHECKLIST_SKILL: _skill_body(_CHECKLIST_SKILL)})
    return "\n\n".join(
        resolver(_CHECKLIST_SKILL, section) for section in _CHECKLIST_SECTIONS
    )


def memory_policy_version() -> str:
    """Prompt-cache identity for the memory proposer's pinned text.

    Hand-bumped via :data:`MEMORY_POLICY_VERSION`, never derived from the
    pinned prose (gr732279): a typo, reflow or example change re-asks every
    decided question on all 13 memory detectors when the version is a
    digest. Bump the constant only when the policy's meaning for the
    proposer judge changes; the test suite pins the prose digest as a
    tripwire so any edit to the pinned sections forces that decision.
    """
    return MEMORY_POLICY_VERSION


def uses_memory_policy(
    detector: str, evidence: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool:
    """Include memory duplicate pairs as well as memory-specific detectors."""
    return (
        detector in _MEMORY_DETECTORS
        or evidence.get("kind") == "memory"
        or payload.get("kind") == "memory"
    )
