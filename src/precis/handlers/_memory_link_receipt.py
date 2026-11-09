"""Net link effects from the mention synchronizer, scoped to one edit call.

The sync hands this collector the links it selects for removal and the links
returned by successful additions. Preview uses the same selection and resolver,
without minting parts, queuing jobs or writing links. Retained mention edges
keep their row (and its ``asserted_fingerprint``) — an edge the rewritten
text still names is not a graph change, not even a remove+re-add the receipt
could hide.
Context-local collection keeps handler instances safe for concurrent requests;
a retry replaces its prior attempt's collector. Rendering lives here so memory
authoring lanes need only a decorator and a preview hook in the handler.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from functools import wraps
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.response import Response
from precis.utils import handle_registry, mentions

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Link
    from precis.utils.mentions import LinkTarget


@dataclass(frozen=True)
class Edge:
    src: int
    dst: int
    pos: int | None
    relation: str
    meta: dict[str, Any]
    chunk_id: int | None = None

    @property
    def key(self) -> tuple[int, int, int | None, str]:
        return self.src, self.dst, self.pos, self.relation

    @classmethod
    def from_link(cls, link: Link) -> Edge:
        return cls(
            link.src_ref_id,
            link.dst_ref_id,
            link.dst_ord,
            link.relation,
            link.meta,
            link.dst_chunk_id,
        )


_COLLECT: ContextVar[dict[int, MentionSync] | None] = ContextVar(
    "memory_edit_link_effects", default=None
)


class MentionSync:
    """One sync attempt's selected links and successful net effects."""

    def __init__(self, store: Store, ref_id: int, *, replace_links: bool) -> None:
        self.store = store
        self.ref_id = ref_id
        self.dry_run = False
        self.collecting = _COLLECT.get() is not None
        self.notices: list[str] = []
        # Only ref-level outgoing sources are touched by the sync's remove
        # and add calls. links_for may also return an inverse-relation row.
        links: list[Link] = (
            [
                link
                for link in store.links_for(
                    ref_id, direction="out", relation="related-to"
                )
                if link.src_ref_id == ref_id and link.src_ord is None
            ]
            if replace_links
            else []
        )
        self.before = {Edge.from_link(link).key: Edge.from_link(link) for link in links}
        self.after = dict(self.before)
        self.removals = [
            link
            for link in links
            if replace_links and (link.meta or {}).get("auto") == "mention"
        ]
        sink = _COLLECT.get()
        if sink is not None:
            sink[ref_id] = self

    def resolve(self, text: str) -> list[LinkTarget]:
        return mentions.resolve_link_targets(
            self.store, text, exclude_ref_id=self.ref_id
        )

    def unmentioned_removals(self, targets: Iterable[LinkTarget]) -> list[Link]:
        """Removal candidates whose target the text no longer names.

        A still-named edge must survive the sync untouched: deleting and
        re-inserting its row would stamp a fresh ``asserted_fingerprint``
        and silently clear the suspect flag with no reviewed re-link
        (gr568170). Only an auto edge the text dropped is removed.
        """
        kept = {(t.dst_ref_id, t.dst_pos) for t in targets}
        return [
            link
            for link in self.removals
            if (link.dst_ref_id, link.dst_ord) not in kept
        ]

    def removed(self, link: Link) -> None:
        self.after.pop(Edge.from_link(link).key, None)

    def added(self, link: Link) -> None:
        edge = Edge.from_link(link)
        self.after[edge.key] = edge

    def preview(self, text: str) -> None:
        from precis.store._links_ops import (
            _machine_edge_admission,
            _machine_write_context,
            _resolve_chunk_id_for_link,
        )

        self.dry_run = True
        for lcsc in mentions.extract_part_lcscs(text):
            if self.store.part_ref_id(lcsc) is None:
                self.notices.append(
                    f"Mention preview: part:{lcsc} has no live ref; apply may mint "
                    "it and add a generated related-to edge."
                )
        targets = self.resolve(text)
        for link in self.unmentioned_removals(targets):
            self.removed(link)
        for target in targets:
            machine_writer = _machine_write_context() is not None
            if target.dst_pos is not None or machine_writer:
                try:
                    with self.store.pool.connection() as conn:
                        chunk_id = _resolve_chunk_id_for_link(
                            conn, target.dst_ref_id, target.dst_pos
                        )
                        if machine_writer:
                            # For the sync's fixed related-to relation this
                            # admission is a pure refusal, never the explains
                            # routing branch that can write a claim edge.
                            _machine_edge_admission(
                                self.store,
                                conn,
                                relation="related-to",
                                src_ref_id=self.ref_id,
                                dst_ref_id=target.dst_ref_id,
                                src_chunk_id=None,
                                dst_chunk_id=chunk_id,
                                meta={"auto": "mention"},
                                set_by="agent",
                            )
                except BadInput as exc:
                    # The best-effort sync stops at the first failed add;
                    # earlier removals/additions still reach its commit.
                    self.notices.append(f"Mention sync would stop: {exc.cause}")
                    break
            edge = Edge(
                self.ref_id,
                target.dst_ref_id,
                target.dst_pos,
                "related-to",
                {"auto": "mention"},
            )
            # add_link's conflict path leaves explicit provenance untouched.
            self.after.setdefault(edge.key, edge)

    def render(self) -> str:
        # Edge-validity keys (asserted_fingerprint, suspect_cleared_*) are
        # store-stamped, not caller meta: add_link refuses them and a
        # restore re-stamps the target's sha fresh anyway (gr568170).
        from precis.store._links_ops import _EDGE_VALIDITY_META_KEYS

        removed = [v for k, v in self.before.items() if k not in self.after]
        added = [v for k, v in self.after.items() if k not in self.before]
        notices = "".join(f"\n\n{notice}" for notice in self.notices)
        if not removed and not added:
            return notices
        refs = self.store.fetch_refs_by_ids(
            sorted({e.src for e in removed + added} | {e.dst for e in removed + added}),
            include_deleted=True,
        )
        lines = ["", "", "Links:"]
        for action, edges in (("remove", removed), ("add", added)):
            verb = (
                f"would {action}"
                if self.dry_run
                else {"remove": "removed", "add": "added"}[action]
            )
            for edge in edges:
                src = refs[edge.src]
                dst = refs[edge.dst]
                source = handle_registry.try_format(src.kind, src.id) or (
                    f"{src.kind}:{src.public_id}"
                )
                target = handle_registry.try_format(dst.kind, dst.id) or (
                    f"{dst.kind}:{dst.public_id}"
                )
                suffix = f"~{edge.pos}" if edge.pos is not None else ""
                origin = (
                    "generated mention edge"
                    if edge.meta.get("auto") == "mention"
                    else "explicit/provenance edge"
                )
                lines.append(
                    f"- {verb} {edge.relation} {source} -> {target}{suffix} ({origin})"
                )
                if action == "remove":
                    address = f"{dst.kind}:{dst.public_id}{suffix}"
                    if "~" in str(dst.public_id) or (
                        edge.pos is not None and edge.pos < 0
                    ):
                        # The link parser reads a slug's ``~`` as a selector
                        # and refuses negative card ordinals; handles do both.
                        address = (
                            target
                            if edge.pos is None
                            else handle_registry.try_format(
                                dst.kind, edge.chunk_id, chunk=True
                            )
                        ) or address
                    restore_meta = {
                        k: v
                        for k, v in edge.meta.items()
                        if k not in _EDGE_VALIDITY_META_KEYS
                    }
                    meta = f", meta={restore_meta!r}" if restore_meta else ""
                    restore = (
                        "Restore after target is live (currently retired)"
                        if dst.retired_at is not None
                        else "Restore"
                    )
                    lines.append(
                        f"  {restore}: link(kind={src.kind!r}, id={src.id}, "
                        f"target={address!r}, rel={edge.relation!r}{meta})"
                    )
        return "\n".join(lines) + notices


def preview_link_effects(store: Store, ref_id: int, text: str) -> None:
    """Resolve the sync's proposed links using read-only paths only."""
    MentionSync(store, ref_id, replace_links=True).preview(text)


def link_effect_receipt[**P](fn: Callable[P, Response]) -> Callable[P, Response]:
    """Append effects only after the edit has completed successfully."""

    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> Response:
        if _COLLECT.get() is not None:
            # Batch edits keep only each nested response's first line.
            # Collect into the outer call and render once after it succeeds.
            return fn(*args, **kwargs)
        effects: dict[int, MentionSync] = {}
        token = _COLLECT.set(effects)
        try:
            response = fn(*args, **kwargs)
            suffix = "".join(effect.render() for effect in effects.values())
            return (
                replace(response, body=response.body + suffix) if suffix else response
            )
        finally:
            _COLLECT.reset(token)

    return wrapped
