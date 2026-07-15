"""QuestHandler — the striving above the work (migration 0065, quest layer).

A **quest is a perpetual, unachievable striving** (the medieval Grail sense):
you never file it ``done``; you strive toward it and it *drives* — pulling
subtasks and knowledge acquisition into its service. It is the **only new kind**
in the model — the achievable structure beneath a quest stays ordinary
todos/projects, marked as serving the quest by a ``serves`` link. Full design:
docs/proposals/quest-layer.md.

Numeric-id ref (like memory/concept/gripe): ``refs.title`` = the striving
statement (+ success criteria); ``refs.meta`` carries ``priority`` (the striving
weight that flows down the ``serves`` DAG as the reweighting field, from slice 2)
and ``horizon``; the ``STATUS:`` tag carries the lifecycle
``active | dormant | abandoned`` (there is no ``done``). ``emits_card=True`` so
the statement is embedded as the reused ``card_combined`` chunk (ord=-1) — a
quest **is a vector**, the substrate the alignment floor + reading calibration
consume for free.

The **logbook** hangs off the quest as append-only ``quest_log`` chunks (the
``gripe`` body+comment pattern) — a WORM, dated ledger of deeds. Append idiom
mirrors gripe's comment append, with an entry type:
``put(kind='quest', id=N, text='…', entry='hypothesis')``. A ``milestone`` entry
is a **deed** (the honest medieval sense of progress); an entry carrying a
``cost`` feeds the **tote** (lifetime spend = a query over the dated log, no
separate cost store). Dead-ends are first-class — recording what failed stops the
whole system re-treading it.

Slice 1 is **read-only structure**: the kind + the ``serves`` relation + the
logbook + the ``view='tree'`` rollup. Nothing steers yet (reweighting is slice
2), so the model is inspectable before anything acts on it.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Ref, Tag
from precis.store.types import Block, BlockInsert
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

#: The append-only logbook chunk_kind (seeded by migration 0065).
_LOG_KIND = "quest_log"

#: Lightly-typed logbook entry vocabulary (docs/proposals/quest-layer.md). A
#: ``milestone`` is a deed; a ``cost`` entry (or any entry with ``meta.cost``)
#: feeds the tote; a ``dead-end`` records what failed so the system stops
#: re-treading it; an un-answered ``hypothesis`` is a gap (slice 3).
_ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "note",
        "observation",
        "hypothesis",
        "result",
        "decision",
        "dead-end",
        "milestone",
        "reflection",
        "cost",
    }
)
_DEFAULT_ENTRY = "note"

#: The perpetual lifecycle. A quest is a striving with NO achieved state — it
#: never completes (docs/proposals/quest-layer.md). STATUS is a shared union
#: axis, so the value-subset is enforced here in the handler, not at the tag
#: parser (which only gates *which* axes a kind may carry).
_LIFECYCLE: frozenset[str] = frozenset({"active", "dormant", "abandoned"})

#: Who authored a logbook entry.
_BY_VALUES: frozenset[str] = frozenset({"human", "agent", "dream"})
_DEFAULT_BY = "human"

#: Recursion guard for the tree rollup — a striving DAG can ladder deep and
#: diamond (a concept serving two routes that both serve one quest); a visited
#: set kills cycles, this caps the depth of sub-quest expansion.
_MAX_TREE_DEPTH = 4


def _status_of(tags: list[Tag]) -> str | None:
    """Extract the ``STATUS:`` value from a tag list (``active`` / …)."""
    for t in tags:
        if t.namespace == "closed" and t.prefix == "STATUS":
            return t.value
    return None


class QuestHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="quest",
        title="Quest",
        description=(
            "A perpetual, unachievable striving (the medieval Grail sense) that "
            "pulls subtasks + knowledge acquisition into its service. Never "
            "`done` — lifecycle is active/dormant/abandoned. Body is the "
            "striving statement (+ criteria). Append logbook entries with "
            "put(id=N, text=…, entry='hypothesis'); mark serving work with "
            "link(target='quest:N', rel='serves'). view='tree' rolls up the "
            "servers + deed ledger. See docs/proposals/quest-layer.md."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_delete=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=True,
        id_required=False,
        note_like=True,
    )

    kind: ClassVar[str] = "quest"
    sense: ClassVar[str] = "quest"

    #: Every quest is born striving.
    default_tags_on_create: ClassVar[tuple[str, ...]] = ("STATUS:active",)

    #: Emit an embeddable ``card_combined`` (the statement) so the quest is a
    #: vector — the substrate for the alignment floor + reading calibration.
    emits_card: ClassVar[bool] = True

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        # /recent (base) + the lifecycle shorthands. `active` is the daily
        # reach ("what are we striving toward?"); dormant/abandoned surface
        # the set-aside + renounced strivings.
        return ("recent", "active", "dormant", "abandoned")

    def _list_view(self, view: str) -> Response | None:
        if view in ("active", "dormant", "abandoned"):
            return self._list_by_tags([f"STATUS:{view}"], page_size=20)
        return super()._list_view(view)

    # ── tag: guard the perpetual lifecycle ──────────────────────────

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        # A quest never completes: reject any STATUS value outside the
        # active/dormant/abandoned lifecycle before the generic tag path
        # (which would happily accept STATUS:done from the shared union).
        for t in add or []:
            if isinstance(t, str) and t.startswith("STATUS:"):
                val = t.split(":", 1)[1].strip()
                if val not in _LIFECYCLE:
                    raise BadInput(
                        f"STATUS:{val} is not a quest lifecycle state — a quest "
                        "is a perpetual striving with no `done`",
                        options=sorted(_LIFECYCLE),
                        next="STATUS: is one of active / dormant / abandoned",
                    )
        return super().tag(id=id, add=add, remove=remove, **_kw)

    # ── put: create or append a logbook entry ───────────────────────

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        entry: str | None = None,
        by: str | None = None,
        cost: float | None = None,
        **_kw: Any,
    ) -> Response:
        # ``put(id=N, text=…)`` appends a quest_log chunk — the logbook-entry
        # idiom for this kind (mirrors gripe's comment append). The base
        # NumericRefHandler.put rejects id-presence unconditionally, so we
        # intercept before delegating.
        if id is not None:
            if text is None or not text.strip():
                raise BadInput(
                    f"appending a logbook entry to {self._sense()} id={id!r} "
                    "requires text=",
                    next=(
                        f"put(kind={self.kind!r}, id={id}, text='what happened', "
                        "entry='observation')"
                    ),
                )
            # Tags / links / mode belong on tag() / link() against the ref,
            # not the append path.
            if tags is not None or untags is not None:
                raise BadInput(
                    "tags=/untags= are not accepted when appending a logbook entry",
                    next=f"use tag(kind={self.kind!r}, id={id}, add=[...]/remove=[...])",
                )
            if link is not None or unlink is not None or rel is not None:
                raise BadInput(
                    "link=/unlink=/rel= are not accepted when appending a "
                    "logbook entry",
                    next=(
                        f"use link(kind={self.kind!r}, id={id}, target='kind:id', "
                        "rel='serves', mode='add')"
                    ),
                )
            if mode is not None:
                raise BadInput(
                    f"mode= is not accepted on {self._sense()} put",
                    next=f"delete(kind={self.kind!r}, id={id})",
                )
            return self._append_log(id=id, text=text, entry=entry, by=by, cost=cost)
        return super().put(
            id=id,
            text=text,
            mode=mode,
            tags=tags,
            untags=untags,
            link=link,
            unlink=unlink,
            rel=rel,
        )

    def _append_log(
        self,
        *,
        id: str | int,
        text: str,
        entry: str | None,
        by: str | None,
        cost: float | None,
    ) -> Response:
        entry_type = (entry or _DEFAULT_ENTRY).strip().lower()
        if entry_type not in _ENTRY_TYPES:
            raise BadInput(
                f"unknown logbook entry type {entry!r}",
                options=sorted(_ENTRY_TYPES),
                next=(
                    "entry= is one of: " + ", ".join(sorted(_ENTRY_TYPES)) + " "
                    "(a milestone is a deed; a cost feeds the tote)"
                ),
            )
        by_who = (by or _DEFAULT_BY).strip().lower()
        if by_who not in _BY_VALUES:
            raise BadInput(
                f"unknown logbook author {by!r}",
                options=sorted(_BY_VALUES),
                next="by= is one of: " + ", ".join(sorted(_BY_VALUES)),
            )
        ref_id = self._coerce_id(id)
        ref = self._resolve_live_ref(ref_id)
        # Next pos = current chunk count. list_blocks_for_ref excludes the
        # synthetic card (ord=-1), so the first logbook entry lands at pos=0.
        next_pos = len(self.store.list_blocks_for_ref(ref.id))
        entry_meta: dict[str, Any] = {
            "chunk_kind": _LOG_KIND,
            "entry_type": entry_type,
            "by": by_who,
        }
        if cost is not None:
            entry_meta["cost"] = float(cost)
        with self.store.tx() as conn:
            self.store.insert_blocks(
                ref.id,
                [BlockInsert(pos=next_pos, text=text, meta=entry_meta)],
                conn=conn,
            )
        deed = " (a deed)" if entry_type == "milestone" else ""
        return Response(
            body=(
                f"logged {entry_type} on {self._sense()} id={ref.id}{deed} "
                f"(entry {next_pos + 1})"
            )
        )

    # ── get: default single-ref + view='tree' rollup ────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        q: str | None = None,
        **_kw: Any,
    ) -> Response:
        # `view='tree'` on a concrete id rolls up the servers + deed ledger.
        # Path-views (id='/active') and the base views (links/log/raw) fall
        # through to NumericRefHandler.get unchanged.
        if (
            view == "tree"
            and id is not None
            and not (isinstance(id, str) and id.startswith("/"))
        ):
            ref = self._resolve_live_ref(self._coerce_id(id))
            return Response(body=self._render_tree(ref))
        return super().get(id=id, view=view, q=q, **_kw)

    # ── rendering ────────────────────────────────────────────────────

    def _log_entries(self, ref_id: int) -> list[Block]:
        """The logbook chunks (quest_log) in append order."""
        return [
            b
            for b in self.store.list_blocks_for_ref(ref_id)
            if b.chunk_kind == _LOG_KIND
        ]

    @staticmethod
    def _tote(entries: list[Block]) -> float:
        """Lifetime spend sunk into the quest — the sum of entry costs."""
        return sum(float((b.meta or {}).get("cost", 0) or 0) for b in entries)

    def _render_one(self, ref: Ref, tags: list[Tag]) -> str:  # type: ignore[override]
        status = _status_of(tags) or "active"
        meta = ref.meta or {}
        lines = [f"# quest {ref.id}: {ref.title.splitlines()[0]}"]
        lines.append(f"striving: {status}")
        if meta.get("priority") is not None:
            lines.append(f"priority: {meta['priority']}")
        if meta.get("horizon"):
            lines.append(f"horizon: {meta['horizon']}")
        if tags:
            lines.append("tags: " + " ".join(str(t) for t in tags))
        # Full statement (title may carry criteria on later lines).
        rest = ref.title.split("\n", 1)
        if len(rest) > 1 and rest[1].strip():
            lines += ["", rest[1].rstrip()]
        # Logbook timeline.
        entries = self._log_entries(ref.id)
        if entries:
            deeds = sum(
                1 for b in entries if (b.meta or {}).get("entry_type") == "milestone"
            )
            tote = self._tote(entries)
            head = f"## logbook — {len(entries)} entr{'y' if len(entries) == 1 else 'ies'}, {deeds} deed{'s' if deeds != 1 else ''}"
            if tote:
                head += f", tote {tote:g}"
            lines += ["", head]
            for b in entries:
                bmeta = b.meta or {}
                etype = bmeta.get("entry_type", "note")
                by = bmeta.get("by", "?")
                stamp = b.created_at.date().isoformat() if b.created_at else "?"
                cost = bmeta.get("cost")
                cost_s = f" cost={cost:g}" if cost else ""
                lines.append(f"\n### {etype} · {stamp} · {by}{cost_s}")
                lines.append(b.text)
        return "\n".join(lines)

    def _render_tree(
        self, ref: Ref, *, depth: int = 0, visited: set[int] | None = None
    ) -> str:
        """Roll up the quest: servers grouped by kind + deed ledger + tote.

        Walks the inbound ``serves`` edges (who is in this quest's service),
        groups servers by kind, and recurses one level into sub-quests so a
        striving DAG renders as a tree. ``visited`` kills cycles; ``depth``
        caps sub-quest expansion.
        """
        visited = visited if visited is not None else set()
        visited.add(ref.id)
        indent = "  " * depth
        tags = self.store.tags_for(ref.id)
        status = _status_of(tags) or "active"
        meta = ref.meta or {}
        prio = f" prio={meta['priority']}" if meta.get("priority") is not None else ""
        handle = handle_registry.try_format(self.kind, ref.id) or f"quest:{ref.id}"
        lines = [f"{indent}◆ {handle} [{status}]{prio} {ref.title.splitlines()[0]}"]

        # Servers, grouped by kind. Inbound `serves` = nodes in this quest's
        # service (one row per edge; not stored mirrored — links_for's inverse
        # rewrite surfaces it from this end).
        server_links = self.store.links_for(ref.id, direction="in", relation="serves")
        server_ids = [ln.src_ref_id for ln in server_links]
        servers = self.store.fetch_refs_by_ids(set(server_ids))
        by_kind: dict[str, list[Ref]] = {}
        sub_quests: list[Ref] = []
        for sid in server_ids:
            s = servers.get(sid)
            if s is None or s.deleted_at is not None:
                continue
            if s.kind == "quest":
                sub_quests.append(s)
            else:
                by_kind.setdefault(s.kind, []).append(s)

        # Sub-quests recurse (the striving ladder) — unless we'd cycle or bust
        # the depth cap, in which case render a leaf pointer.
        for sq in sub_quests:
            if sq.id in visited or depth + 1 >= _MAX_TREE_DEPTH:
                sqh = handle_registry.try_format("quest", sq.id) or f"quest:{sq.id}"
                lines.append(f"{indent}  ◆ {sqh} {sq.title.splitlines()[0]} …")
                continue
            lines.append(self._render_tree(sq, depth=depth + 1, visited=visited))

        # Other servers, one group per kind.
        for kind in sorted(by_kind):
            refs = by_kind[kind]
            lines.append(f"{indent}  {kind} ({len(refs)}) serving:")
            for s in refs:
                sh = handle_registry.try_format(s.kind, s.id) or f"{s.kind}:{s.id}"
                title = (s.title or "").splitlines()[0] if s.title else ""
                lines.append(f"{indent}    ▸ {sh} {title[:70]}")

        # Deed ledger + tote (top-level only — a rollup, not per-node noise).
        if depth == 0:
            entries = self._log_entries(ref.id)
            deeds = [
                b for b in entries if (b.meta or {}).get("entry_type") == "milestone"
            ]
            tote = self._tote(entries)
            open_hyps = sum(
                1 for b in entries if (b.meta or {}).get("entry_type") == "hypothesis"
            )
            lines += ["", "── ledger ──"]
            lines.append(
                f"{len(entries)} logbook entr{'y' if len(entries) == 1 else 'ies'} · "
                f"{len(deeds)} deed{'s' if len(deeds) != 1 else ''} · "
                f"{open_hyps} hypothes{'is' if open_hyps == 1 else 'es'} · "
                f"tote {tote:g}"
            )
            for b in deeds[-8:]:
                stamp = b.created_at.date().isoformat() if b.created_at else "?"
                lines.append(f"  ✦ {stamp}  {b.text.splitlines()[0][:80]}")
            if not server_ids:
                lines += render_next_section(
                    [
                        (
                            f"link(kind='todo', id=N, target={handle!r}, rel='serves')",
                            "put a project/goal in this quest's service",
                        ),
                        (
                            f"put(kind={self.kind!r}, id={ref.id}, text='…', entry='observation')",
                            "record a logbook entry",
                        ),
                    ]
                )
        return "\n".join(lines)

    def _render_create_ack(self, ref_id: int) -> Response:
        handle = handle_registry.try_format(self.kind, ref_id) or f"id={ref_id}"
        body = f"created quest {handle} (STATUS:active)."
        body += render_next_section(
            [
                (
                    f"link(kind='todo', id=N, target={self.kind!r}+':{ref_id}', rel='serves')",
                    "put a project/goal in its service",
                ),
                (
                    f"put(kind={self.kind!r}, id={ref_id}, text='…', entry='hypothesis')",
                    "add a logbook entry",
                ),
                (
                    f"get(kind={self.kind!r}, id={ref_id}, view='tree')",
                    "roll up its servers + deed ledger",
                ),
            ]
        )
        return Response(body=body)


__all__ = ["QuestHandler"]
