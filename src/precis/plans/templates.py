"""Plan templates: functions that mint a todo subtree (dark-factory, td542095).

Spec: ``docs/backlog/dark-factory.md`` § "Plans" → "Templates". A template
is registered in :data:`TEMPLATES` under its name; ``put(kind='todo',
mode='plan', args={'template': name, ...})`` calls it.

Every minted todo

- is shaped by :mod:`precis.plans.nodes` (checked with
  :func:`~precis.plans.nodes.validate_plan_meta` before the insert),
- carries no ``meta.llm_tier`` -- the ``{'llm_tier': None}`` opt-out of
  ``precis-todo-tree-help``, applied as the handler applies it (the key is
  dropped: a stored null would still satisfy dispatch's ``meta ? 'llm_tier'``
  check) -- so ``plan_tick`` never auto-dispatches a plan node; the plan
  dispatcher does,
- is tagged ``plan:<root id>`` (the root too), and
- is sequenced by ``blocked-by`` links (``src`` is the blocked node).

The whole subtree is minted in one transaction. A loop's ``body``
('release-fix', 'pcb-fix') names the iteration the tick re-mints; it is not a
key of :data:`TEMPLATES`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.plans.nodes import validate_plan_meta
from precis.store import Store, Tag
from precis.store.types import TODO_BODY_KIND, ChunkInsert
from precis.utils import handle_registry

#: ``meta.gate.args.sha`` of a ``sha-in-prod`` gate minted before the build
#: that fills it has landed (valid hex, matches no commit).
PENDING_SHA = "0000000"
GATE_SHA_NOTE = (
    "The sha in meta.gate is a placeholder ('0000000'): the build task's land "
    "writes the real sha here once it is known. Until then this gate stays false."
)
_RELEASE_RANGE = "origin/prod..main"
_RELEASE_LOOP_MAX = 3
_PCB_LOOP_MAX = 5
_GRIPE_ID_RE = re.compile(r"(?:gr|gripe:)?(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class PlanMint:
    """What a template minted: the plan root and a ``key -> todo id`` map
    (the root is ``ids['root']``)."""

    root_id: int
    ids: dict[str, int]


@dataclass(frozen=True)
class Template:
    """A registered plan template.

    ``required`` names the arguments ``mint`` needs (the handler checks them
    before the call); ``mint(store, parent_id=None, **args)`` mints the
    subtree under ``parent_id`` (a new root when ``None``).
    """

    name: str
    required: tuple[str, ...]
    summary: str
    mint: Callable[..., PlanMint]


class _Minter:
    """Mints one plan's todos inside a single transaction."""

    def __init__(self, store: Store, conn: Any, parent_id: int | None) -> None:
        self.store = store
        self.conn = conn
        self.parent_id = parent_id
        self.root_id: int | None = None
        self.ids: dict[str, int] = {}
        #: ``(todo id, text)`` comments to append once the tx has committed.
        self.comments: list[tuple[int, str]] = []

    def node(
        self,
        key: str,
        title: str,
        meta: dict[str, Any],
        *,
        parent: int | None = None,
        body: str | None = None,
        blocked_by: tuple[str, ...] = (),
        comment: str | None = None,
    ) -> int:
        problems = validate_plan_meta(meta)
        if problems:
            raise BadInput(
                f"plan template minted an invalid node {key!r}: " + "; ".join(problems),
                next="this is a template defect; file a gripe naming the template",
            )
        is_root = self.root_id is None
        ref = self.store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=dict(meta),
            parent_id=self.parent_id if is_root else (parent or self.root_id),
            conn=self.conn,
        )
        if is_root:
            self.root_id = ref.id
        tags = ["STATUS:open", f"plan:{self.root_id}"]
        for tag in tags:
            self.store.add_tag(
                ref.id,
                Tag.parse_strict(tag, kind="todo"),
                set_by="agent",
                replace_prefix=tag.startswith("STATUS:"),
                conn=self.conn,
            )
        if body:
            self.store.chunks.insert_chunks(
                ref.id,
                [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
                conn=self.conn,
            )
        for dep in blocked_by:
            self.store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=self.ids[dep],
                relation="blocked-by",
                conn=self.conn,
            )
        if comment:
            self.comments.append((ref.id, comment))
        self.ids[key] = ref.id
        return ref.id

    def result(self) -> PlanMint:
        assert self.root_id is not None
        return PlanMint(root_id=self.root_id, ids=dict(self.ids))


def _run(
    store: Store,
    parent_id: int | None,
    build: Callable[[_Minter], None],
    *,
    related_to: int | None = None,
) -> PlanMint:
    """Run ``build`` in one transaction, then post the deferred comments."""
    with store.tx() as conn:
        m = _Minter(store, conn, parent_id)
        build(m)
        if related_to is not None:
            store.add_link(
                src_ref_id=m.ids["root"],
                dst_ref_id=related_to,
                relation="related-to",
                conn=conn,
            )
    if m.comments:
        from precis.handlers import _comment_timeline as tl

        for todo_id, text in m.comments:
            ref = store.get_ref(kind="todo", id=todo_id)
            assert ref is not None
            tl.append_comment(
                store, ref, kind="todo", comment_kind="todo_comment", text=text
            )
    return m.result()


def _phase() -> dict[str, Any]:
    return {"auto_close": True}


def _task(op: Any = "run_slice") -> dict[str, Any]:
    return {"op": op}


def _generate(role: str, source: str) -> dict[str, Any]:
    return {"generate": {"role": role, "from": source}}


# -- gripe-fix ---------------------------------------------------------------


def _gripe_id(raw: Any) -> int:
    m = _GRIPE_ID_RE.fullmatch(str(raw).strip())
    if m is None:
        raise BadInput(
            f"gripe_id={raw!r} is not a gripe id",
            next="args={'template': 'gripe-fix', 'gripe_id': 123} (or 'gr123')",
        )
    return int(m.group(1))


def mint_gripe_fix(
    store: Store, parent_id: int | None = None, *, gripe_id: Any
) -> PlanMint:
    """triage -> brief -> build -> gate sha-in-prod -> validate, under a phase
    root that is ``related-to`` the gripe (the gripe keeps the ticket state)."""
    gid = _gripe_id(gripe_id)
    gripe = store.get_ref(kind="gripe", id=gid)
    if gripe is None:
        raise BadInput(
            f"gripe {gid} not found",
            next="search(kind='gripe', q='...') to find the gripe id",
        )
    handle = handle_registry.format_handle("gripe", gid)
    label = f"{handle}: {str(gripe.title).strip()[:80]}"

    def build(m: _Minter) -> None:
        m.node("root", f"Fix {label}", _phase())
        m.node(
            "triage",
            f"Triage {handle}",
            _generate("triage", handle),
            body=(
                f"Read {handle}: pick the task class and the builder model; "
                "mint an ask gate when the report is unusable."
            ),
        )
        m.node(
            "brief",
            f"Write the builder brief for {handle}",
            _task(),
            body=f"A frontier model writes the builder's brief chunk for {handle}.",
            blocked_by=("triage",),
        )
        m.node(
            "build",
            f"Build the fix for {handle}",
            _task("run_slice"),
            body=(
                f"run_slice on the lane triage picked; the land writes "
                f"'resolved: <sha>' + verify on {handle}."
            ),
            blocked_by=("brief",),
        )
        m.node(
            "gate",
            f"Wait for the {handle} fix to reach prod",
            {"gate": {"kind": "sha-in-prod", "args": {"sha": PENDING_SHA}}},
            blocked_by=("build",),
            comment=GATE_SHA_NOTE,
        )
        m.node(
            "validate",
            f"Validate the {handle} fix",
            _task(),
            body=(
                "Re-run the repro or test the gripe names; an LLM read of the "
                "report against the landed change otherwise."
            ),
            blocked_by=("gate",),
        )

    return _run(store, parent_id, build, related_to=gripe.id)


# -- slice-tree --------------------------------------------------------------


def mint_slice_tree(
    store: Store, parent_id: int | None = None, *, section: str
) -> PlanMint:
    """spec-writer -> slicer -> (N tasks, minted by the slicer) -> review.

    The spec-writer generator always exists: when the section is not thin its
    turn mints nothing. The review is blocked-by the slicer, which behaves as
    a phase once it has minted its tasks, so it waits for all of them."""
    section = str(section).strip()
    if not section:
        raise BadInput(
            "section is empty",
            next="args={'template': 'slice-tree', 'section': 'main/docs/backlog/x.md~Heading'}",
        )

    def build(m: _Minter) -> None:
        m.node("root", f"Slice {section}", _phase())
        m.node(
            "spec",
            f"Write the spec for {section}",
            _generate("spec-writer", section),
            body="Thicken the section into a buildable spec; mint nothing when it already is.",
        )
        m.node(
            "slicer",
            f"Slice {section} into tasks",
            _generate("slicer", section),
            body=(
                "Mint one task per slice, each with a declared file set; "
                "sequence overlapping file sets with blocked-by."
            ),
            blocked_by=("spec",),
        )
        m.node(
            "review",
            f"Review the slices of {section}",
            _task(),
            body="Review the landed slices on another lane than the builders'.",
            blocked_by=("slicer",),
        )

    return _run(store, parent_id, build)


# -- release-pass ------------------------------------------------------------


def mint_release_pass(store: Store, parent_id: int | None = None) -> PlanMint:
    """cut -> gate tests-green; a loop mints fixer + re-cut while red.

    The gate and the loop are siblings, both blocked-by the cut: the gate
    shows the predicate, the loop's ``until`` is the same predicate and it
    starts empty (a green cut never runs a fixer)."""
    gate = {"kind": "tests-green", "args": {"range": _RELEASE_RANGE}}

    def build(m: _Minter) -> None:
        m.node("root", "Release pass", _phase())
        m.node(
            "cut",
            "Cut the release",
            _task(),
            body="Cut release/r<N> from main with scripts/release.",
        )
        m.node(
            "gate",
            "Wait for the cut to go green",
            {"gate": gate},
            blocked_by=("cut",),
        )
        m.node(
            "loop",
            "Fix and re-cut until green",
            {"loop": {"max": _RELEASE_LOOP_MAX, "until": gate, "body": "release-fix"}},
            blocked_by=("cut",),
        )

    return _run(store, parent_id, build)


# -- pcb-pipeline ------------------------------------------------------------


def mint_pcb_pipeline(
    store: Store, parent_id: int | None = None, *, board: str
) -> PlanMint:
    """place -> a loop whose first iteration is route -> drc -> review, until
    the DRC is clean (max 5)."""
    board = str(board).strip()
    if not board:
        raise BadInput(
            "board is empty",
            next="args={'template': 'pcb-pipeline', 'board': 'pcb:<slug>'}",
        )

    def build(m: _Minter) -> None:
        m.node("root", f"PCB pipeline for {board}", _phase())
        m.node(
            "place",
            f"Place {board}",
            _task({"type": "pcb_place", "params": {"board": board}}),
            body=f"Run pcb_place on {board}.",
        )
        loop = m.node(
            "loop",
            f"Route and check {board} until the DRC is clean",
            {
                "loop": {
                    "max": _PCB_LOOP_MAX,
                    # No drc-clean gate kind exists; tests-green over a
                    # 'drc:<board>' range is the closest shape (to be settled
                    # with the gate evaluators).
                    "until": {"kind": "tests-green", "args": {"range": f"drc:{board}"}},
                    "body": "pcb-fix",
                }
            },
            blocked_by=("place",),
        )
        # First iteration at mint (the worked example's loop L holds its first
        # body); the tick re-mints a body named 'pcb-fix' while the DRC is dirty.
        m.node(
            "route",
            f"Route {board}",
            _task(),  # no route job_type yet (dark-factory Templates); swap when added
            parent=loop,
            body=f"Route {board}.",
        )
        m.node(
            "drc",
            f"DRC {board}",
            _task({"type": "pcb_review", "params": {"board": board}}),
            parent=loop,
            body=f"Run the DRC check leg of pcb_review on {board}.",
            blocked_by=("route",),
        )
        m.node(
            "review",
            f"Review the DRC report of {board}",
            _generate("triage", board),
            parent=loop,
            body="Read the DRC report; mint a fix task per violation.",
            blocked_by=("drc",),
        )

    return _run(store, parent_id, build)


TEMPLATES: dict[str, Template] = {
    t.name: t
    for t in (
        Template(
            "gripe-fix",
            ("gripe_id",),
            "triage -> brief -> build -> sha-in-prod gate -> validate, for one gripe",
            mint_gripe_fix,
        ),
        Template(
            "slice-tree",
            ("section",),
            "spec-writer -> slicer -> review, for an md section address",
            mint_slice_tree,
        ),
        Template(
            "release-pass",
            (),
            "cut -> tests-green gate; fixer + re-cut loop while red",
            mint_release_pass,
        ),
        Template(
            "pcb-pipeline",
            ("board",),
            "place -> route/drc/review loop until the DRC is clean",
            mint_pcb_pipeline,
        ),
    )
}
