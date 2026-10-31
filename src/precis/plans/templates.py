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
('release-fix', 'pcb-fix') names the iteration the tick re-mints; it is a
key of :data:`BODIES`, not of :data:`TEMPLATES`. :func:`mint_body` mints one
iteration under the loop todo, tagged with the loop's own ``plan:<root>``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput, NotFound
from precis.plans import release as _release
from precis.plans.gates import resolve_board
from precis.plans.nodes import validate_plan_meta
from precis.store import Store, Tag
from precis.store.types import TODO_BODY_KIND, ChunkInsert
from precis.todo.guards import check_ask_meta
from precis.utils import handle_registry

#: ``meta.gate.args.sha`` of a ``sha-in-prod`` gate minted before the build
#: that fills it has landed (valid hex, matches no commit).
PENDING_SHA = "0000000"
GATE_SHA_NOTE = (
    "The sha in meta.gate is a placeholder ('0000000'): the build task's land "
    "writes the real sha here once it is known. Until then this gate stays false."
)
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
        #: The ``plan:<id>`` tag value when it is not ``root_id`` (a loop
        #: body mints under the loop but belongs to the loop's plan).
        self.plan_root: int | None = None
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
        tags = ["STATUS:open", f"plan:{self.plan_root or self.root_id}"]
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
    _post_comments(store, m.comments)
    return m.result()


def _post_comments(store: Store, comments: list[tuple[int, str]]) -> None:
    """Append the minter's deferred ``(todo id, text)`` comments."""
    if not comments:
        return
    from precis.handlers import _comment_timeline as tl

    for todo_id, text in comments:
        ref = store.get_ref(kind="todo", id=todo_id)
        assert ref is not None
        tl.append_comment(
            store, ref, kind="todo", comment_kind="todo_comment", text=text
        )


def _phase() -> dict[str, Any]:
    return {"auto_close": True}


def _task(op: Any = "run_slice") -> dict[str, Any]:
    return {"op": op}


def _generate(role: str, source: str) -> dict[str, Any]:
    return {"generate": {"role": role, "from": source}}


#: ``meta.task_class`` of a reviewer node (``run_slice`` picks its lane by it).
REVIEW_TASK_CLASS = "review"

#: The verdict lines a reviewer posts; ``plan_dispatch`` parses them.
REVIEW_GRAMMAR = (
    "`review: LAND` (clean) | `review: FIX — <the list of findings>` | "
    "`review: REVERT — <why>`"
)


def _reviewer(target_id: int) -> dict[str, Any]:
    """A reviewer generator over the land of ``td<target_id>``."""
    return {**_generate("reviewer", f"td{target_id}"), "task_class": REVIEW_TASK_CLASS}


def _reviewer_brief(target_id: int, *, slices: bool = False) -> str:
    """The reviewer's brief (the node body ``run_slice`` puts in the session
    brief): read the landed squash diff against the brief, post one verdict."""
    if slices:
        what = (
            f"Each child todo of td{target_id} is a slice that landed on main: "
            "its `landed: <sha>` comment names the squash commit. For each, "
            "read `git show <sha>` against that child's brief chunk "
            "(`get(kind='todo', id=<child>)`)."
        )
    else:
        what = (
            f"td{target_id} landed on main: its `landed: <sha>` comment names "
            "the squash commit. Read `git show <sha>` against td"
            f"{target_id}'s brief chunk (`get(kind='todo', id={target_id})`)."
        )
    return (
        "Review after land, from another lane than the builder's. "
        f"{what} Judge whether the diff does what the brief asked, and "
        "nothing it should not.\n\n"
        "Post exactly one comment on this todo, "
        "`put(kind='todo', id=<this todo>, text=...)`, as one of: "
        f"{REVIEW_GRAMMAR}. The text after the dash is what the fix or the "
        "revert slice is built from: name files and the concrete problem.\n\n"
        "Mint nothing yourself: no child todos, no fix, no revert, no commit, "
        "no land, and do not tag this todo done. The plan tick reads the "
        "verdict: LAND closes this review, FIX mints the fix slice, REVERT "
        "mints the revert slice and requeues the build."
    )


def mint_plan_task(
    store: Store, plan_root: int, title: str, body: str, *, op: Any = "run_slice"
) -> int:
    """Mint one task todo directly under ``plan_root``, tagged
    ``plan:<plan_root>`` (the tick's fix / revert slices). Returns its id."""

    def build(m: _Minter) -> None:
        m.node("task", title, _task(op), body=body)

    return _run_body(store, plan_root, plan_root, build).ids["task"]


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
    root that is ``related-to`` the gripe (the gripe keeps the ticket state).

    A reviewer generator over the build's land is blocked-by the build only:
    the review is non-blocking, so the gate and the validate do not wait on
    it (dark-factory.md § "Branches")."""
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
            "review",
            f"Review the {handle} fix after land",
            _reviewer(m.ids["build"]),
            body=_reviewer_brief(m.ids["build"]),
            blocked_by=("build",),
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
    turn mints nothing. The review is a reviewer generator over the slicer's
    tasks, blocked-by the slicer, which behaves as a phase once it has minted
    its tasks, so it waits for all of them."""
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
            _reviewer(m.ids["slicer"]),
            body=_reviewer_brief(m.ids["slicer"], slices=True),
            blocked_by=("slicer",),
        )

    return _run(store, parent_id, build)


# -- release-pass ------------------------------------------------------------


#: Retry budget of the verdict poll: each job parks itself while CI is
#: pending (``ci_verdict_poll``), so a retry is another hour of polling.
_POLL_RETRIES = 3
RELEASE_RANGE_NOTE = (
    f"The range in meta.gate is a placeholder ('{_release.PENDING_RANGE}'): the "
    "release_cut job writes the cut's '<prev prod>..<sha>' here (and the sha "
    "and r<N> on the plan root's meta.release). Until then this gate stays false."
)


def _tag(m: _Minter, todo_id: int, value: str) -> None:
    m.store.add_tag(
        todo_id, Tag.parse_strict(value, kind="todo"), set_by="agent", conn=m.conn
    )


def mint_release_pass(store: Store, parent_id: int | None = None) -> PlanMint:
    """cut -> poll -> gate tests-green -> approval ask -> deploy; a loop
    mints fixer + re-cut while red (td543733).

    ``cut`` (``release_cut``) runs ``scripts/release --cut-only`` and stamps
    r<N>/sha on the root (``meta.release``) and the real range on the gate
    and the loop's ``until``. ``poll`` (``ci_verdict_poll``) records the
    GitHub verdict, parking itself while CI runs. The gate and the loop are
    siblings blocked-by the poll, so the loop sees a recorded verdict: green
    closes it empty, red mints a fixer. ``approval`` is an ``answer`` gate
    that is also the ask-user todo Reto answers (``deploy r<N>`` | ``hold``);
    ``deploy`` (``release_deploy``) refuses unless that answer names
    ``deploy r<N>`` (:func:`precis.plans.release.approval`)."""
    gate = {"kind": "tests-green", "args": {"range": _release.PENDING_RANGE}}
    ask: dict[str, Any] = {
        "kind": "approval",
        "options": _release.approval_options("r<N>"),
        "blocks": "nothing",
        "context": "release/r<N> is cut by this plan; green CI gates this ask",
    }

    def build(m: _Minter) -> None:
        m.node("root", "Release pass", _phase())
        m.node(
            "cut",
            "Cut the release",
            _task("release_cut"),
            body=(
                "release_cut runs scripts/release --cut-only in the repo checkout: "
                "cut release/r<N> from main and return without waiting for CI."
            ),
        )
        m.node(
            "poll",
            "Record the CI verdict on the cut",
            {**_task("ci_verdict_poll"), "retries": _POLL_RETRIES},
            body=(
                "ci_verdict_poll reads check.yml's run on the cut's sha from the "
                "GitHub API and records it in release.ci_verdicts."
            ),
            blocked_by=("cut",),
        )
        m.node(
            "gate",
            "Wait for the cut to go green",
            {"gate": gate},
            blocked_by=("poll",),
            comment=RELEASE_RANGE_NOTE,
        )
        m.node(
            "loop",
            "Fix and re-cut until green",
            {"loop": {"max": _RELEASE_LOOP_MAX, "until": gate, "body": "release-fix"}},
            blocked_by=("poll",),
        )
        approval = m.node(
            "approval",
            "Deploy the release? (answer 'deploy r<N>' or 'hold')",
            {"gate": {"kind": "answer"}, "ask": ask},
            body=(
                "The release this plan cut is green. Answer with "
                "put(kind='todo', id=<this>, text='answer: deploy r<N>') and close "
                "this todo; 'hold' (or any other answer) deploys nothing."
            ),
            blocked_by=("gate",),
        )
        _tag(m, approval, "ask-user")
        deploy = m.node(
            "deploy",
            "Deploy the release",
            _task("release_deploy"),
            body=(
                "release_deploy runs scripts/release --deploy-only r<N> (pinned), "
                "only after the approval ask is answered 'deploy r<N>'."
            ),
            blocked_by=("approval",),
        )
        final = {**ask, "blocks": f"td{deploy}"}
        check_ask_meta({"ask": final})
        m.store.stamp_ref_meta(approval, {"ask": final}, conn=m.conn)

    return _run(store, parent_id, build)


# -- pcb-pipeline ------------------------------------------------------------


def _pcb_board(store: Store, raw: Any, *, where: str) -> tuple[int, str]:
    """``(pcb ref id, 'pb<id>' handle)`` for a board spelling; refuses one
    that names no live pcb design."""
    try:
        ref_id = resolve_board(store, raw)
    except (BadInput, NotFound) as e:
        raise BadInput(
            f"{where}: board={raw!r} names no pcb design ({e})",
            next=(
                "search(kind='pcb', q='...') for the design, then "
                "args={'template': 'pcb-pipeline', 'board': 'pb<id>'}"
            ),
        ) from e
    return ref_id, handle_registry.format_handle("pcb", ref_id)


def _drc_until(board: str) -> dict[str, Any]:
    return {"kind": "drc-clean", "args": {"board": board}}


def mint_pcb_pipeline(
    store: Store, parent_id: int | None = None, *, board: Any
) -> PlanMint:
    """place+route -> a loop whose first iteration is drc -> review, until
    the DRC is clean (max 5). ``board`` is resolved to its pcb ref at mint:
    the job ops carry ``pcb_ref_id``, the gate and titles the ``pb<id>``
    handle."""
    ref_id, handle = _pcb_board(store, board, where="pcb-pipeline")
    params = {"pcb_ref_id": ref_id}

    def build(m: _Minter) -> None:
        m.node("root", f"PCB pipeline for {handle}", _phase())
        m.node(
            "place",
            f"Place {handle}",
            # Place and route are one job: the topo router runs only inside
            # pcb_place (placer='topo', route=True).
            _task(
                {
                    "type": "pcb_place",
                    "params": {**params, "placer": "topo", "route": True},
                }
            ),
            body=f"Run pcb_place (topo placer, route on) on {handle}.",
        )
        loop = m.node(
            "loop",
            f"Check and fix {handle} until the DRC is clean",
            {
                "loop": {
                    "max": _PCB_LOOP_MAX,
                    "until": _drc_until(handle),
                    "body": "pcb-fix",
                }
            },
            blocked_by=("place",),
        )
        # First iteration at mint (the worked example's loop L holds its first
        # body); the tick re-mints a body named 'pcb-fix' while the DRC is dirty.
        m.node(
            "drc",
            f"DRC {handle}",
            _task({"type": "pcb_review", "params": dict(params)}),
            parent=loop,
            body=f"Run the DRC check leg of pcb_review on {handle}.",
            blocked_by=("place",),
        )
        m.node(
            "review",
            f"Review the DRC report of {handle}",
            _generate("triage", handle),
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
            "triage -> brief -> build -> sha-in-prod gate -> validate "
            "(+ a non-blocking review of the land), for one gripe",
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
            "cut -> CI verdict poll -> tests-green gate -> deploy approval ask -> "
            "deploy; fixer + re-cut loop while red",
            mint_release_pass,
        ),
        Template(
            "pcb-pipeline",
            ("board",),
            "place+route -> drc/review loop until the DRC is clean",
            mint_pcb_pipeline,
        ),
    )
}


# -- loop bodies -------------------------------------------------------------


def _loop_context(store: Store, loop_todo_id: int) -> tuple[dict[str, Any], int]:
    """``(meta.loop, plan root id)`` of a loop todo. The root is the id in
    the loop's ``plan:<id>`` tag; a hand-made loop without one is its own
    root."""
    ref = store.get_ref(kind="todo", id=loop_todo_id)
    if ref is None:
        raise BadInput(
            f"loop todo {loop_todo_id} not found",
            next="pass the id of a live todo carrying meta.loop",
        )
    loop = (ref.meta or {}).get("loop")
    if not isinstance(loop, dict):
        raise BadInput(
            f"td{loop_todo_id} carries no meta.loop",
            next="mint a body only under a loop node (meta.loop = {max, until, body})",
        )
    root = loop_todo_id
    for tag in store.tags_for(loop_todo_id):
        value = str(tag.value)
        if value.startswith("plan:") and value[5:].isdigit():
            root = int(value[5:])
            break
    return loop, root


def _run_body(
    store: Store, loop_todo_id: int, plan_root: int, build: Callable[[_Minter], None]
) -> PlanMint:
    """Mint one loop iteration under ``loop_todo_id`` in one transaction."""
    with store.tx() as conn:
        m = _Minter(store, conn, loop_todo_id)
        m.root_id = loop_todo_id  # every node parents to the loop
        m.plan_root = plan_root
        build(m)
    _post_comments(store, m.comments)
    return PlanMint(root_id=loop_todo_id, ids=dict(m.ids))


def _iteration_suffix(iteration: Any) -> str:
    return f" (iteration {iteration})" if iteration else ""


def mint_release_fix(
    store: Store, loop_todo_id: int, *, iteration: int | None = None, **_ctx: Any
) -> PlanMint:
    """triage (generator over gripes tagged ``plan:<root>``) -> fix -> re-cut."""
    _loop, root = _loop_context(store, loop_todo_id)
    tag = f"plan:{root}"
    suffix = _iteration_suffix(iteration)

    def build(m: _Minter) -> None:
        m.node(
            "triage",
            f"Triage the red release's gripes{suffix}",
            _generate("triage", f"gripes tagged {tag}"),
            body=(
                f"Read the open gripes tagged {tag} and the red check output; "
                "mint one fix task per independent failure, or an ask gate when "
                "a report is unusable."
            ),
        )
        m.node(
            "fix",
            f"Fix what triage found{suffix}",
            _task(),
            body=(
                f"Fix the failures triage named for {tag}; land each fix with "
                "'resolved: <sha>' on its gripe."
            ),
            blocked_by=("triage",),
        )
        m.node(
            "recut",
            f"Re-cut the release{suffix}",
            _task(),
            body="Cut release/r<N> from main with scripts/release.",
            blocked_by=("fix",),
        )

    return _run_body(store, loop_todo_id, root, build)


def mint_pcb_fix(
    store: Store, loop_todo_id: int, *, iteration: int | None = None, **_ctx: Any
) -> PlanMint:
    """fix -> drc -> review generator, for the board the loop's ``until``
    gate names (``drc-clean {board: 'pb<id>'}``; a loop minted before that
    kind existed carries ``tests-green {range: 'drc:<board>'}``, also read)."""
    loop, root = _loop_context(store, loop_todo_id)
    until = loop.get("until") if isinstance(loop.get("until"), dict) else {}
    args = (until or {}).get("args") or {}
    if (until or {}).get("kind") == "drc-clean":
        raw = str(args.get("board") or "").strip()
    else:
        rng = str(args.get("range") or "")
        raw = rng[4:].strip() if rng.startswith("drc:") else ""
    if not raw:
        raise BadInput(
            f"pcb-fix on td{loop_todo_id}: meta.loop.until names no board "
            f"(want {{'kind': 'drc-clean', 'args': {{'board': 'pb<id>'}}}}, "
            f"got {until!r})",
            next="mint the loop with the pcb-pipeline template",
        )
    ref_id, board = _pcb_board(store, raw, where=f"pcb-fix on td{loop_todo_id}")
    suffix = _iteration_suffix(iteration)

    def build(m: _Minter) -> None:
        m.node(
            "fix",
            f"Fix the DRC violations of {board}{suffix}",
            _task(),
            body=(
                f"Fix the violations the last DRC review of {board} listed; "
                "re-route where a fix needs it."
            ),
        )
        m.node(
            "drc",
            f"DRC {board}{suffix}",
            _task({"type": "pcb_review", "params": {"pcb_ref_id": ref_id}}),
            body=f"Run the DRC check leg of pcb_review on {board}.",
            blocked_by=("fix",),
        )
        m.node(
            "review",
            f"Review the DRC report of {board}{suffix}",
            _generate("triage", board),
            body="Read the DRC report; mint a fix task per violation.",
            blocked_by=("drc",),
        )

    return _run_body(store, loop_todo_id, root, build)


#: Loop body name -> minter ``(store, loop_todo_id, **ctx) -> PlanMint``. The
#: tick passes ``iteration=<n>``; a body ignores context it does not use.
BODIES: dict[str, Callable[..., PlanMint]] = {
    "release-fix": mint_release_fix,
    "pcb-fix": mint_pcb_fix,
}


def mint_body(store: Store, loop_todo_id: int, name: str, **ctx: Any) -> PlanMint:
    """Mint one iteration of loop body ``name`` under ``loop_todo_id``."""
    minter = BODIES.get(name)
    if minter is None:
        raise BadInput(
            f"no loop body named {name!r}",
            next="name a body registered in precis.plans.templates.BODIES",
            options=sorted(BODIES),
        )
    return minter(store, loop_todo_id, **ctx)
