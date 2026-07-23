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
logbook + the ``view='tree'`` rollup. Slice 2 (:mod:`precis.quest.reweight`)
flows priority down the ``serves`` DAG. Slice 3 (:mod:`precis.quest.gaps`) makes
the striving expose its own **exploration queue** — ``view='tree'`` now also
rolls up *health* (momentum + an embedding alignment floor) and *gaps* (thin
support, no literature, low-mastery served concepts, un-answered hypotheses);
``view='gaps'`` (per quest) and ``id='/gaps'`` (corpus-wide) surface just that
queue.
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput, Unsupported
from precis.handlers._numeric_ref import _BASE_VIEWS, NumericRefHandler
from precis.protocol import KindSpec
from precis.quest.logbook import (
    BY_VALUES as _BY_VALUES,
)
from precis.quest.logbook import (
    DEFAULT_BY as _DEFAULT_BY,
)
from precis.quest.logbook import (
    DEFAULT_ENTRY as _DEFAULT_ENTRY,
)
from precis.quest.logbook import (
    ENTRY_TYPES as _ENTRY_TYPES,
)
from precis.quest.logbook import (
    LOG_KIND as _LOG_KIND,
)
from precis.quest.logbook import append_entry as _append_logbook_entry
from precis.response import Response
from precis.store import Ref, Tag
from precis.store.types import Block
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

#: The perpetual lifecycle. A quest is a striving with NO achieved state — it
#: never completes (docs/proposals/quest-layer.md). STATUS is a shared union
#: axis, so the value-subset is enforced here in the handler, not at the tag
#: parser (which only gates *which* axes a kind may carry).
_LIFECYCLE: frozenset[str] = frozenset({"active", "dormant", "abandoned"})

#: ``PRIO:`` tag → the canonical ``refs.prio`` column (1..10, lower = hotter) —
#: the same striving-weight scale the todo tree rotates on and slice 2's
#: reweighting reads (:mod:`precis.quest.reweight`). Mirrors the todo handler's
#: back-compat map so a quest's priority is a real column, not a decorative tag.
_PRIO_TAG_TO_INT: dict[str, int] = {
    "PRIO:urgent": 1,
    "PRIO:high": 3,
    "PRIO:normal": 5,
    "PRIO:low": 8,
}


def _split_prio(tags: list[str] | None) -> tuple[list[str] | None, int | None]:
    """Pull the last ``PRIO:`` tag out of ``tags`` and translate it to an int.

    Returns ``(tags_without_prio, prio_or_none)`` — the ``PRIO:`` alias is
    stripped so it never lands as a redundant closed-tag row alongside the
    column write. Unknown ``PRIO:`` values pass through untouched so the strict
    validator surfaces the typo with its options list."""
    if not tags:
        return tags, None
    out: list[str] = []
    found: int | None = None
    for t in tags:
        if t in _PRIO_TAG_TO_INT:
            found = _PRIO_TAG_TO_INT[t]
            continue
        out.append(t)
    return (out if out else None), found


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


#: Quest-specific ``get(view=…)`` tokens on a concrete id, handled below
#: before the base ``links/log/raw`` fall-through. Kept here so the
#: unknown-view error can enumerate them — the base class only knows its own
#: ``_BASE_VIEWS`` and would otherwise mislead a caller into thinking
#: links/log/raw are the *only* quest views (they aren't).
_QUEST_CONCRETE_VIEWS: tuple[str, ...] = (
    "tree",
    "gaps",
    "dossier",
    "frontier",
    "leaderboard",
)


class QuestHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="quest",
        title="Quest",
        description=(
            "A perpetual, unachievable striving (the medieval Grail sense) that "
            "pulls subtasks + knowledge acquisition into its service. Never "
            "`done` — lifecycle is active/dormant/abandoned. Body is the "
            "striving statement (+ criteria). Append logbook entries with "
            "put(id=N, text=…, entry='hypothesis'); rewrite the founding "
            "statement in place with edit(id=N, mode='replace', text=…) "
            "(logbook/links/tags untouched); mark serving work with "
            "link(target='quest:N', rel='serves'). view='tree' rolls up the "
            "servers + deed ledger + health + gaps; view='gaps' (per quest) or "
            "id='/gaps' (all active quests) surfaces the exploration queue; "
            "view='dossier' shows the living research synthesis; view='frontier' "
            "the Pareto frontier of candidate materials (banded); "
            "view='leaderboard' the same frontier as a TOON design table. "
            "See docs/proposals/quest-layer.md."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_edit=True,
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

    #: Ref id of the most recent create, captured in ``_render_create_ack`` so
    #: the ``_create`` override can apply a create-time ``PRIO:`` to the column
    #: after the base transaction commits (the base insert doesn't take prio).
    _last_created_id: int | None = None

    # ── create: sync a create-time PRIO: into the prio column ────────

    def _create(  # type: ignore[override]
        self,
        *,
        text: str | None,
        tags: list[str] | None,
        link: str | None,
        rel: str | None = None,
        auto_refresh_days: int | None = None,
    ) -> Response:
        tags, prio_from_tag = _split_prio(tags)
        resp = super()._create(
            text=text,
            tags=tags,
            link=link,
            rel=rel,
            auto_refresh_days=auto_refresh_days,
        )
        if prio_from_tag is not None and self._last_created_id is not None:
            self.store.set_prio(self._last_created_id, prio_from_tag)
        return resp

    # ── list-view filters (id='/<view>') ────────────────────────────

    def _supported_list_views(self) -> tuple[str, ...]:
        # /recent (base) + the lifecycle shorthands + /gaps (the corpus-wide
        # exploration queue). `active` is the daily reach ("what are we striving
        # toward?"); dormant/abandoned surface the set-aside + renounced
        # strivings; /gaps rolls up what is thin across every active quest.
        return ("recent", "active", "dormant", "abandoned", "gaps")

    def _list_view(self, view: str) -> Response | None:
        if view in ("active", "dormant", "abandoned"):
            return self._list_by_tags([f"STATUS:{view}"], page_size=20)
        if view == "gaps":
            return self._render_gaps_dashboard()
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
        # ``PRIO:`` → the canonical prio column (the striving weight slice 2's
        # reweighting reads), stripped from the tag set like todo does. A bare
        # ``PRIO:*`` in remove clears the column.
        add, prio_from_tag = _split_prio(add)
        clear_prio = False
        if remove:
            kept = [t for t in remove if t not in _PRIO_TAG_TO_INT]
            clear_prio = len(kept) != len(remove)
            remove = kept or None
        if prio_from_tag is not None or clear_prio:
            ref_id = self._coerce_id(id)
            self._resolve_live_ref(ref_id)
            self.store.set_prio(ref_id, None if clear_prio else prio_from_tag)
            if not add and not remove:
                # Only a prio write happened — the base tag() would reject an
                # otherwise-empty call.
                return Response(
                    body=(
                        f"set prio={None if clear_prio else prio_from_tag} "
                        f"on {self._sense()} id={ref_id}"
                    )
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
        # Shared append path (precis.quest.logbook) — the same insert the
        # autonomous quest_tick writes through, so there is one logbook writer.
        entry_no = _append_logbook_entry(
            self.store, ref.id, text=text, entry_type=entry_type, by=by_who, cost=cost
        )
        deed = " (a deed)" if entry_type == "milestone" else ""
        return Response(
            body=(
                f"logged {entry_type} on {self._sense()} id={ref.id}{deed} "
                f"(entry {entry_no})"
            )
        )

    # ── edit: in-place rewrite of the founding striving statement ────

    def edit(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        mode: str = "replace",
        text: str | None = None,
        **_kw: Any,
    ) -> Response:
        """In-place rewrite of the founding striving statement (``refs.title``).

        Only ``mode='replace'`` is supported. Mirrors ``memory``/``todo``'s
        edit: same id, same ``STATUS:``/``PRIO:`` tags, same logbook entries,
        same ``serves``/``served-by`` links — only the statement text
        changes. The old wording lands in ``ref_events`` as a
        ``body_replaced`` row (``view='log'`` for the diff). Distinct from
        the logbook append (``put(id=N, text=…, entry=…)``), which never
        touches the founding text; this is the "wordsmith the striving,
        keep everything else" verb (gripe 169979).
        """
        if id is None:
            raise BadInput(
                "edit(kind='quest') requires id=",
                next="edit(kind='quest', id=N, mode='replace', text='new striving statement')",
            )
        if mode != "replace":
            raise BadInput(
                f"edit(kind='quest') only supports mode='replace', got {mode!r}",
                next=(
                    "edit(kind='quest', id=N, mode='replace', "
                    "text='new striving statement')"
                ),
            )
        if text is None or not text.strip():
            raise BadInput(
                "edit(kind='quest', mode='replace') requires text=",
                next=(
                    "edit(kind='quest', id=N, mode='replace', "
                    "text='new striving statement')"
                ),
            )
        ref_id = self._coerce_id(id)
        ref = self._resolve_live_ref(ref_id)
        with self.store.tx() as conn:
            old_text = self.store.replace_ref_text(
                ref.id, text, source="agent", conn=conn
            )
            if self.emits_card:
                # Re-embed the card so search/alignment reads the rewritten
                # statement, not the stale one (DELETE+INSERT re-enters the
                # embed worker's queue).
                self.store.upsert_card_combined(ref.id, text, conn=conn)
        old_words = len((old_text or "").split())
        new_words = len(text.split())
        return Response(
            body=(
                f"replaced striving statement of {self._sense()} id={ref.id} "
                f"({old_words} → {new_words} words). "
                "logbook/links/tags untouched. view='log' for the full diff."
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
        # `view='tree'` on a concrete id rolls up the servers + deed ledger +
        # health + gaps; `view='gaps'` is the focused exploration queue for one
        # quest. Path-views (id='/active', id='/gaps') and the base views
        # (links/log/raw) fall through to NumericRefHandler.get unchanged.
        concrete = id is not None and not (isinstance(id, str) and id.startswith("/"))
        if view == "tree" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))  # type: ignore[arg-type]
            return Response(body=self._render_tree(ref))
        if view == "gaps" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))  # type: ignore[arg-type]
            return Response(body=self._render_gaps_only(ref))
        if view == "dossier" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))  # type: ignore[arg-type]
            return Response(body=self._render_dossier(ref))
        if view == "frontier" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))  # type: ignore[arg-type]
            return Response(body=self._render_frontier(ref))
        if view == "leaderboard" and concrete:
            ref = self._resolve_live_ref(self._coerce_id(id))  # type: ignore[arg-type]
            return Response(body=self._render_leaderboard(ref))
        # An unrecognised view on a concrete id would otherwise fall through to
        # NumericRefHandler.get, whose error lists only links/log/raw — hiding
        # the five quest views above. "logbook"/"deeds" are the two shapes a
        # caller reaches for (the skill prose is saturated with both words), so
        # name them explicitly: the logbook shows by default, deeds are a
        # filtered slice of view='log'. (Tooling-log audit: recurring guess.)
        if concrete and view is not None and view not in _BASE_VIEWS:
            raise Unsupported(
                f"unknown view {view!r} for kind='quest'",
                options=[*_QUEST_CONCRETE_VIEWS, *_BASE_VIEWS],
                next=[
                    "quest views: tree, gaps, dossier, frontier, leaderboard "
                    "(quest-specific) · links, log, raw (generic)",
                    "no 'logbook'/'deeds' view — the logbook shows by default "
                    "(get(kind='quest', id=N)); use view='log' for the raw ledger",
                ],
            )
        return super().get(id=id, view=view, q=q, **_kw)

    def _render_frontier(self, ref: Ref) -> str:
        """`view='frontier'` — the Pareto frontier of candidate materials."""
        from precis.quest import frontier as frontier_mod

        fr = frontier_mod.quest_frontier(self.store, ref.id)
        head = ref.title.splitlines()[0] if ref.title else f"quest {ref.id}"
        objs = " · ".join(f"{k} ({s})" for k, s in fr.objectives)
        lines = [f"# frontier — quest {ref.id}: {head}", f"objective: {objs}", ""]

        def _fmt(c: frontier_mod.Candidate) -> str:
            ms = " ".join(f"{k}={v:g}" for k, v in sorted(c.measures.items()))
            graduated = any(
                str(t) == "needs-experiment" for t in self.store.tags_for(c.ref_id)
            )
            star = " ★ needs-experiment" if graduated else ""
            return f"  {c.handle} {c.name} — {ms or '(no measures)'}{star}"

        if not (fr.frontier or fr.dominated or fr.unevaluated):
            lines.append("no candidate structures serve this quest yet.")
            return "\n".join(lines)
        lines.append(f"── Pareto frontier ({len(fr.frontier)}) — current best ──")
        lines += [_fmt(c) for c in fr.frontier] or ["  (none converged yet)"]
        if fr.dominated:
            lines += ["", f"── dominated ({len(fr.dominated)}) — explored + beaten ──"]
            lines += [_fmt(c) for c in fr.dominated]
        if fr.unevaluated:
            lines += ["", f"── awaiting a sim ({len(fr.unevaluated)}) ──"]
            lines += [f"  {c.handle} {c.name}" for c in fr.unevaluated]
        return "\n".join(lines)

    def _render_leaderboard(self, ref: Ref) -> str:
        """`view='leaderboard'` — the by-total design leaderboard as a TOON table.

        One row per candidate design (identity · objective vector · Pareto band ·
        graduation flag), sorted best-first per band. This is the LLM-legible
        counterpart of ``view='frontier'`` (which is the banded human summary);
        both render the same :func:`quest_frontier`, so there is no second
        ranking to drift.
        """
        from precis.format import toon
        from precis.quest import frontier as frontier_mod

        fr = frontier_mod.quest_frontier(self.store, ref.id)
        head = ref.title.splitlines()[0] if ref.title else f"quest {ref.id}"
        objs = " · ".join(f"{k} ({s})" for k, s in fr.objectives)
        if not (fr.frontier or fr.dominated or fr.unevaluated):
            return (
                f"# leaderboard — quest {ref.id}: {head}\nobjective: {objs}\n\n"
                "no candidate structures serve this quest yet."
            )
        graduated = {
            c.ref_id
            for c in (*fr.frontier, *fr.dominated, *fr.unevaluated)
            if any(str(t) == "needs-experiment" for t in self.store.tags_for(c.ref_id))
        }
        rows, schema = frontier_mod.leaderboard(fr, graduated=graduated)
        body = toon.dump(rows, schema=schema)
        return f"# leaderboard — quest {ref.id}: {head}\nobjective: {objs}\n\n{body}"

    def _render_dossier(self, ref: Ref) -> str:
        """`view='dossier'` — the quest's living research synthesis (slice 4)."""
        from precis.quest import dossier as dossier_mod

        did, _handle, text = dossier_mod.read_dossier(self.store, ref.id)
        head = ref.title.splitlines()[0] if ref.title else f"quest {ref.id}"
        if did is None:
            return (
                f"# dossier — quest {ref.id}: {head}\n\n"
                "(no dossier yet — a quest tick creates + fills it)"
            )
        dh = handle_registry.try_format("draft", did) or f"draft:{did}"
        return f"# dossier {dh} — quest {ref.id}: {head}\n\n{text}"

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
        if ref.prio is not None:
            # The canonical striving weight (prio 1=hottest…10; slice 2's
            # reweighting flows this down the serves DAG). Set via PRIO: tag.
            lines.append(f"priority: {ref.prio}")
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
                # Full timestamp (date + UTC time to the minute) — the logbook
                # is the quest's append-only lab notebook, so entries want a
                # real clock, not just a date, to read as a chronological record.
                stamp = b.created_at.strftime("%Y-%m-%d %H:%M") if b.created_at else "?"
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
        prio = f" prio={ref.prio}" if ref.prio is not None else ""
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
        live_servers: list[Ref] = []
        _seen: set[int] = set()
        for sid in server_ids:
            s = servers.get(sid)
            if s is None or s.deleted_at is not None or sid in _seen:
                continue
            _seen.add(sid)
            live_servers.append(s)
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
            # Health (momentum + alignment floor) + gaps — the striving's own
            # exploration queue (slice 3, precis.quest.gaps).
            lines += self._render_health_and_gaps(ref, live_servers)
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

    # ── slice 3: health (momentum + alignment) + gaps ────────────────

    def _render_health_and_gaps(self, ref: Ref, live_servers: list[Ref]) -> list[str]:
        """Momentum + alignment-floor + the gap list, for the tree/gaps views."""
        from precis.quest import gaps as gapmod

        out: list[str] = []
        health = gapmod.quest_health(self.store, ref.id, servers=live_servers)
        m = health.momentum
        out += ["", "── health ──"]
        momentum = (
            f"momentum: {m.label} · {m.recent_entries} recent log · "
            f"{m.recent_server_events} server event"
            f"{'' if m.recent_server_events == 1 else 's'}/{gapmod.MOMENTUM_WINDOW_DAYS}d"
        )
        if m.open_todo_servers:
            momentum += (
                f" · {m.open_todo_servers} open todo"
                f"{'' if m.open_todo_servers == 1 else 's'}"
            )
        if m.blocked_todo_servers:
            momentum += f" · {m.blocked_todo_servers} blocked (child-failed)"
        out.append(momentum)
        if health.alignment_checked:
            if health.alignment_flags:
                out.append(
                    f"alignment: {len(health.alignment_flags)} of "
                    f"{health.alignment_checked} embedded servers drifting off-aim:"
                )
                for fl in health.alignment_flags[:6]:
                    out.append(f"  ~ {fl.handle} cos={fl.cosine:.2f} {fl.title}")
            else:
                out.append(
                    f"alignment: all {health.alignment_checked} embedded servers on-aim"
                )

        gap_list = gapmod.quest_gaps(self.store, ref.id, servers=live_servers)
        if gap_list:
            out += ["", f"── gaps ({len(gap_list)}) — the exploration queue ──"]
            for g in gap_list:
                where = f"  [{g.handle}]" if g.handle else ""
                out.append(f"  ▫ {g.kind}: {g.detail}{where}")
        return out

    def _render_gaps_only(self, ref: Ref) -> str:
        """`view='gaps'` on one quest — its health + focused exploration queue."""
        from precis.quest import gaps as gapmod

        live = gapmod._live_servers(self.store, ref.id)
        handle = handle_registry.try_format(self.kind, ref.id) or f"quest:{ref.id}"
        lines = [f"# gaps — {handle}: {ref.title.splitlines()[0]}"]
        lines += self._render_health_and_gaps(ref, live)
        if not gapmod.quest_gaps(self.store, ref.id, servers=live):
            lines += ["", "no gaps — this striving is well-supported."]
        return "\n".join(lines)

    def _render_gaps_dashboard(self) -> Response:
        """`id='/gaps'` — the exploration queue across every active quest."""
        from precis.quest import gaps as gapmod

        with self.store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id FROM refs r "
                "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
                "JOIN tags t ON t.tag_id = rt.tag_id "
                "WHERE r.kind = 'quest' AND r.deleted_at IS NULL "
                "AND t.namespace = 'STATUS' AND t.value = 'active' "
                "ORDER BY COALESCE(r.prio, 5) ASC, r.ref_id ASC"
            ).fetchall()
        ids = [int(r[0]) for r in rows]
        if not ids:
            return Response(body="no active quests — nothing to surface gaps for.")
        lines = ["# gaps across active quests — the exploration queue", ""]
        total = 0
        for qid in ids:
            ref = self._resolve_live_ref(qid)
            live = gapmod._live_servers(self.store, qid)
            gs = gapmod.quest_gaps(self.store, qid, servers=live)
            m = gapmod.quest_momentum(self.store, qid, servers=live)
            total += len(gs)
            handle = handle_registry.try_format("quest", qid) or f"quest:{qid}"
            plural = "" if len(gs) == 1 else "s"
            lines.append(
                f"◆ {handle} [{m.label}] {ref.title.splitlines()[0]} "
                f"— {len(gs)} gap{plural}"
            )
            for g in gs[:4]:
                where = f"  [{g.handle}]" if g.handle else ""
                lines.append(f"    ▫ {g.kind}: {g.detail[:80]}{where}")
        qn = "" if len(ids) == 1 else "s"
        gn = "" if total == 1 else "s"
        lines.insert(1, f"{len(ids)} active quest{qn} · {total} gap{gn} total")
        return Response(body="\n".join(lines))

    def _render_create_ack(self, ref_id: int) -> Response:
        # Capture the id so the ``_create`` override can apply a create-time
        # ``PRIO:`` to the prio column after the base transaction commits.
        self._last_created_id = ref_id
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
