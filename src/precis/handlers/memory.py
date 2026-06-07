"""MemoryHandler — capture notes, decisions, ideas, questions.

Numeric-id ref kind. Refactored in phase 5 to subclass
:class:`NumericRefHandler` — the shared CRUD shape now lives in one
place across memory / todo / gripe / fc / conv.

Semantics from the `precis-memory-help` skill:
    - put(text=...)                — create new memory, return its id
    - tag(id=N, add=[...])         — add/replace tags on memory N
    - tag(id=N, remove=[...])      — remove tags from memory N
    - link(id=N, target='kind:id') — cross-link memory N to another ref
    - delete(id=N)                 — soft-delete memory N
    - get(id=N)                    — read memory text + tags
    - get(id='/recent')            — list recent memories
    - search(q=...)            — lexical search over memories
"""

from __future__ import annotations

from typing import Any, ClassVar

from precis.errors import BadInput
from precis.handlers._numeric_ref import NumericRefHandler
from precis.protocol import KindSpec
from precis.response import Response
from precis.store import Tag

#: Max memories that one ``supersede`` call may fold into a survivor.
#: A guardrail, not a quota — the agent can do several small merges.
#: Bounds the blast radius / review cost of a single consolidation;
#: a 30-way merge is almost always over-eager.
_SUPERSEDE_MAX_MERGE = 10

#: Provenance tag forced onto every supersede survivor.
_DREAM_CONSOLIDATED = Tag.closed("DREAM", "consolidated")


class MemoryHandler(NumericRefHandler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="memory",
        title="Memory",
        description=(
            "Notes, decisions, ideas, questions. Numeric id assigned on "
            "create. Sub-kind via 'kind:' open tag."
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

    kind: ClassVar[str] = "memory"
    sense: ClassVar[str] = "memory"

    # Memories become embeddable: put-create emits a `card_combined`
    # chunk (ord=-1) so the embed worker vectorizes it and
    # `search(like=...)` finds true semantic neighbours. Foundation for
    # the dreaming capability (docs/design/dreaming.md).
    emits_card: ClassVar[bool] = True

    # ── supersede: the one guarded destructive verb (dreaming) ──────

    def supersede(
        self,
        *,
        merge_ids: list[Any] | None = None,
        new_text: str | None = None,
        new_tags: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Consolidate >=2 near-duplicate memories into one survivor.

        The single guarded compress-only merge a dream uses instead of
        raw ``delete`` (docs/design/dreaming.md, §Consolidate). In one
        transaction: mint a new ``memory`` (+ ``card_combined`` chunk +
        merged tags), migrate every link off each original onto the
        survivor, add ``survivor --supersedes--> original`` edges, stamp
        ``meta.superseded_by`` and soft-delete the originals.

        Hard guards (enforced here, never the prompt):

        - ``merge_ids``: 2..10 *distinct* live ``memory`` ids. Papers
          (or any non-memory kind) are refused — papers are never
          merged or deleted.
        - ``new_text``: required, and **compress-only** — no longer than
          the combined originals (a merge may forget a nuance, never
          invent a claim).

        A bad call raises a typed ``BadInput`` the agent can read and
        retry; it can never corrupt or hard-delete.
        """
        if not merge_ids or not isinstance(merge_ids, list):
            raise BadInput(
                "supersede requires merge_ids=[id, id, ...] (>= 2 memory ids)",
                next="supersede(merge_ids=[12, 47], new_text='merged wording')",
            )
        # Coerce + dedup preserving order; a repeated id is a mistake,
        # not a 2-way merge.
        seen_ids: set[int] = set()
        ids: list[int] = []
        for raw in merge_ids:
            mid = self._coerce_id(raw)
            if mid not in seen_ids:
                seen_ids.add(mid)
                ids.append(mid)
        if len(ids) < 2:
            raise BadInput(
                f"supersede needs >= 2 distinct memory ids, got {len(ids)}",
                next="pick two or more different memories to merge",
            )
        if len(ids) > _SUPERSEDE_MAX_MERGE:
            raise BadInput(
                f"supersede caps at {_SUPERSEDE_MAX_MERGE} memories per merge, "
                f"got {len(ids)}",
                next="split into smaller, reviewable merges",
            )
        if new_text is None or not new_text.strip():
            raise BadInput(
                "supersede requires new_text= (the consolidated memory)",
                next="supersede(merge_ids=[...], new_text='the merged wording')",
            )

        # Every id must resolve to a *live memory*. get_ref(kind='memory')
        # returns None for a wrong kind, a missing id, or a soft-deleted
        # row — all three are caller errors here.
        originals = []
        for mid in ids:
            ref = self.store.get_ref(kind="memory", id=mid)
            if ref is None:
                raise BadInput(
                    f"supersede: id={mid} is not a live memory "
                    "(wrong kind, missing, or already deleted)",
                    next=f"get(kind='memory', id={mid}) to check",
                )
            originals.append(ref)

        # Compress-only: the survivor may not be longer than the sum of
        # the originals it absorbs. Forgetting a nuance is the accepted
        # loss; inventing new claims is not, and length is the cheap
        # proxy the tool can enforce.
        combined_len = sum(len(r.title or "") for r in originals)
        if len(new_text) > combined_len:
            raise BadInput(
                f"supersede is compress-only: new_text ({len(new_text)} chars) "
                f"exceeds the combined originals ({combined_len} chars)",
                next="shorten new_text — a merge compresses, it never expands",
            )

        # Resolve the tag set before touching the DB so a bad explicit
        # tag fails before any write. Default = union of the originals'
        # OPEN tags (control/closed tags like STATUS:/DREAM: are dropped);
        # the survivor always carries DREAM:consolidated.
        if new_tags is not None:
            tag_objs = [Tag.parse_strict(t, kind="memory") for t in new_tags]
        else:
            tag_objs = []
            seen_tags: set[str] = set()
            for r in originals:
                for t in self.store.tags_for(r.id):
                    if t.namespace != "open":
                        continue
                    key = str(t)
                    if key not in seen_tags:
                        seen_tags.add(key)
                        tag_objs.append(t)
        if not any(str(t) == str(_DREAM_CONSOLIDATED) for t in tag_objs):
            tag_objs.append(_DREAM_CONSOLIDATED)

        with self.store.tx() as conn:
            survivor = self.store.insert_ref(
                kind="memory",
                slug=None,
                title=new_text,
                meta={"superseded": ids},
                conn=conn,
            )
            self.store.upsert_card_combined(survivor.id, new_text, conn=conn)
            for tag in tag_objs:
                self.store.add_tag(
                    survivor.id,
                    tag,
                    set_by="agent",
                    replace_prefix=(tag.namespace == "closed"),
                    conn=conn,
                )
            for mid in ids:
                self.store.migrate_links(mid, survivor.id, conn=conn)
                self.store.add_link(
                    src_ref_id=survivor.id,
                    dst_ref_id=mid,
                    relation="supersedes",
                    set_by="agent",
                    conn=conn,
                )
                self.store.stamp_ref_meta(
                    mid, {"superseded_by": survivor.id}, conn=conn
                )
                self.store.soft_delete_ref(mid, conn=conn)

        merged = ", ".join(str(m) for m in ids)
        return Response(
            body=(
                f"superseded memories [{merged}] → new memory id={survivor.id} "
                f"(originals soft-deleted, links migrated, tagged "
                f"{_DREAM_CONSOLIDATED})"
            )
        )
