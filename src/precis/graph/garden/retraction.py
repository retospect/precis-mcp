"""Propose and apply a verdict on a revision of one citing block."""

from typing import Any

from precis.graph.garden.candidates import GardenCandidate
from precis.graph.garden.proposals import (
    GardenProposal,
    emit_proposal,
    resolve_proposal,
)
from precis.store import Store
from precis.store._draft_ops import DraftChunk, content_sha
from precis.store._retraction_ops import RetractionNotice, Verdict

ACTION = "retraction_verdict"


def propose(
    store: Store,
    chunk: DraftChunk,
    paper_id: int,
    notice: RetractionNotice,
    verdict: Verdict,
    reason: str,
    submitter: str,
) -> int | None:
    sha = content_sha(chunk.text)
    candidate = GardenCandidate(
        detector=ACTION,
        action=ACTION,
        target_kind="chunk",
        target_id=chunk.chunk_id,
        title=f"pa{notice.id} at {chunk.dc}: {verdict}",
        evidence={
            "citation": chunk.text,
            "notice": notice.title,
            "analysis": notice.analysis,
            "analysed_by": notice.analysed_by,
        },
        payload={
            "paper_id": paper_id,
            "notice_id": notice.id,
            "base_sha": sha,
            "verdict": verdict,
            "submitted_by": submitter,
        },
    )
    return emit_proposal(
        store,
        candidate,
        rationale=reason,
        key=f"{candidate.key}:{paper_id}:{notice.id}:{sha}",
    )


def eligible(prop: GardenProposal) -> bool:
    return (
        prop.detector == ACTION
        and prop.target_kind == "chunk"
        and isinstance(prop.payload.get("paper_id"), int)
        and isinstance(prop.payload.get("notice_id"), int)
        and isinstance(prop.payload.get("base_sha"), str)
        and prop.payload.get("verdict") in ("unaffected", "affects")
        and bool(prop.rationale.strip())
    )


class RetractionApplyRefused(ValueError):
    """The proposed verdict can no longer be applied."""


def apply(
    store: Store,
    prop: GardenProposal,
    *,
    actor: str,
    model: str | None,
    extra: dict[str, Any],
) -> None:
    if not eligible(prop) or not prop.current:
        raise RetractionApplyRefused("The proposed verdict is stale or incomplete.")
    payload = prop.payload
    with store.pool.connection() as conn, conn.transaction():
        resolved = resolve_proposal(
            store,
            prop.review_id,
            actor=actor,
            model=model,
            verdict="approved",
            extra=extra,
            conn=conn,
        )
        if resolved is None or not store.judge_retraction_use(
            payload["notice_id"],
            payload["paper_id"],
            prop.target_id,
            payload["base_sha"],
            payload["verdict"],
            prop.rationale,
            model or actor,
            conn=conn,
        ):
            raise RetractionApplyRefused(
                "The citation changed or already has a decision."
            )
