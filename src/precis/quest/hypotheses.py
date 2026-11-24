"""Persist tick conjectures through the finding proposal gate."""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.errors import NotFound
from precis.quest.directions import direction_ref_ids
from precis.quest.logbook import append_entry
from precis.utils import handle_registry

if TYPE_CHECKING:
    from precis.store import Ref, Store


def mint_tick_hypotheses(
    store: Store,
    quest_id: int,
    items: list[object],
    *,
    models: list[str],
    by: str,
) -> int:
    """Mint or reuse findings, with atomic service links and a logbook pointer."""
    from precis.handlers._finding_hypothesis import _propose

    added = 0
    for index, item in enumerate(items[:4], start=1):
        try:
            if not isinstance(item, dict):
                raise ValueError("expected an object")
            sentence = item.get("sentence")
            motivation = item.get("motivation")
            testable_by = item.get("testable_by")
            if not (
                isinstance(sentence, str)
                and isinstance(motivation, str)
                and isinstance(testable_by, str)
            ):
                raise ValueError("sentence, motivation and testable_by must be strings")
            sources = item.get("motivated_by")
            if not isinstance(sources, list) or not all(
                isinstance(h, str) for h in sources
            ):
                raise ValueError("motivated_by must be a list of source handles")
            direction = item.get("direction")
            with store.atomic() as conn:
                direction_id = None
                if direction is not None:
                    parsed = (
                        handle_registry.parse(direction)
                        if isinstance(direction, str)
                        else None
                    )
                    if (
                        parsed is None
                        or parsed[:2] != ("todo", False)
                        or parsed[2] not in direction_ref_ids(store, quest_id)
                    ):
                        raise ValueError(
                            "direction must name a live direction todo in this quest"
                        )
                    direction_id = parsed[2]

                _, fid = _propose(
                    store,
                    sentence=sentence,
                    scope=None,
                    motivation=motivation,
                    testable_by=testable_by,
                    motivated_by=sources,
                    llm_models=models,
                    from_memory=None,
                    set_by="agent",
                    mint_cap=None,
                    meta=None,
                    reuse_published=True,
                )
                conn.execute(
                    "SELECT ref_id FROM refs WHERE ref_id = %s FOR UPDATE", (fid,)
                )
                served = {
                    edge.dst_ref_id
                    for edge in store.links_for(fid, direction="out", relation="serves")
                }
                for target in (quest_id, direction_id):
                    if target is not None and target not in served:
                        store.add_link(
                            src_ref_id=fid,
                            dst_ref_id=target,
                            relation="serves",
                            conn=conn,
                        )
                if quest_id in served and (
                    direction_id is None or direction_id in served
                ):
                    continue
                append_entry(
                    store, quest_id, text=f"raised fi{fid}", entry_type="note", by=by
                )
        except (NotFound, ValueError) as exc:
            append_entry(
                store,
                quest_id,
                text=f"hypothesis {index} skipped — {str(exc).splitlines()[0][:220]}",
                entry_type="note",
                by=by,
            )
        added += 1
    return added


def is_hypothesis(ref: Ref) -> bool:
    return (
        ref.kind == "finding" and (ref.meta or {}).get("artifact_type") == "hypothesis"
    )


def render_tick_hypotheses(store: Store, quest_id: int) -> str:
    """Render served findings and their current evidence and verdicts."""
    from precis.quest.dossier import read_registered_experiments
    from precis.quest.gaps import _live_servers, _open_hypothesis_findings
    from precis.store import Tag

    live = _live_servers(store, quest_id)
    open_findings, governed = _open_hypothesis_findings(store, quest_id, live)
    open_ids = {ref.id for ref in open_findings}
    lines = []
    for ref in live:
        if ref.id not in governed or Tag.closed("STATUS", "refuted") in store.tags_for(
            ref.id
        ):
            continue
        edges = store.links_for(ref.id, direction="in")
        supports = sum(edge.relation == "supports" for edge in edges)
        disputes = sum(edge.relation in {"disputes", "contradicts"} for edge in edges)
        verdict = (
            "open"
            if ref.id in open_ids
            else (ref.meta or {}).get("hypothesis_verdict") or "settled"
        )
        lines.append(
            f"- [fi{ref.id}] {ref.title} — {verdict}; supports: {supports}, disputes: {disputes}"
        )
    text = "\n".join(lines) or "- (none)"
    experiments = read_registered_experiments(
        store, quest_id, governed=governed, open_ids=open_ids
    )
    if experiments:
        text += f"\n\n## Registered experiments\n{experiments}"
    return text
