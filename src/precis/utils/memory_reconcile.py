"""Load the memory writer's canonical reconciliation policy for garden judges.

The packaged skill section is the source of truth. Reusing the skill include
resolver keeps interactive guidance and machine prompts identical; a digest
lets memory-only proposer rejections reopen when that policy changes.

The detector set stays explicit to avoid a proposer → memory_reconcile →
proposer initialization cycle. Regression coverage follows MEMORY_ACTIONS so
new memory detectors cannot silently omit the policy.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Any

from precis.ingest.skill_template import DocResolver

_SKILL = "precis-memory-help"
_SECTION = "memory-reconciliation-policy"
_MEMORY_DETECTORS = frozenset(
    {
        "anchor_rot",
        "dangling_mention",
        "entity_claim_stale",
        "negative_undated",
        "restated_claim",
        "review_due",
        "source_drift",
        "thread_shape",
        "title_shape",
    }
)


@cache
def memory_reconciliation_policy() -> str:
    """The exact packaged H2 section, using the skill include grammar."""
    raw = (
        resources.files("precis.data")
        .joinpath("skills", f"{_SKILL}.md")
        .read_text(encoding="utf-8")
    )
    return DocResolver(docs={_SKILL: raw})(_SKILL, _SECTION)


def memory_policy_version() -> str:
    """Prompt-cache identity for this policy's exact text."""
    digest = hashlib.sha256(memory_reconciliation_policy().encode("utf-8")).hexdigest()
    return f"garden-propose.memory.v3.{digest[:12]}"


def uses_memory_policy(
    detector: str, evidence: Mapping[str, Any], payload: Mapping[str, Any]
) -> bool:
    """Include memory duplicate pairs as well as memory-specific detectors."""
    return (
        detector in _MEMORY_DETECTORS
        or evidence.get("kind") == "memory"
        or payload.get("kind") == "memory"
    )
