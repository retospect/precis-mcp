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
('release-fix', 'pcb-fix', 'route-fix') names the iteration the tick
re-mints; it is a key of :data:`BODIES`, not of :data:`TEMPLATES`.
:func:`mint_body` mints one iteration under the loop todo, tagged with the
loop's own ``plan:<root>``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput, NotFound, Upstream
from precis.plans import pr_class
from precis.plans import release as _release
from precis.plans.gates import resolve_board
from precis.plans.logbook import first_line, log_plan_event
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
    before the call); ``optional`` the ones it passes through when given;
    ``mint(store, parent_id=None, **args)`` mints the subtree under
    ``parent_id`` (a new root when ``None``).
    """

    name: str
    required: tuple[str, ...]
    summary: str
    mint: Callable[..., PlanMint]
    optional: tuple[str, ...] = ()


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
        #: The minted root's title (the "plan opened" logbook line, gr548800).
        self.root_title: str = ""
        #: ``(todo id, title)`` of nodes minted carrying ``meta.ask`` — each
        #: gets an "ask raised" logbook line post-commit (gr548800).
        self.ask_nodes: list[tuple[int, str]] = []
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
            self.root_title = title
        if isinstance(meta.get("ask"), dict):
            self.ask_nodes.append((ref.id, title))
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
    _log_plan_events(store, m)
    return m.result()


def _log_plan_events(store: Store, m: _Minter) -> None:
    """gr548800: the "plan opened" line plus one "ask raised" line per
    minted ask node, appended on the quest above the plan root (a no-op
    for a plan no quest serves). Post-commit: the parent chain must be
    readable to resolve the quest."""
    root = m.result().root_id
    log_plan_event(store, root, f"opened — {first_line(m.root_title)}", root=root)
    for ask_id, title in m.ask_nodes:
        log_plan_event(
            store, root, f"ask td{ask_id} raised — {first_line(title)}", root=root
        )


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


#: ``meta.task_class`` picks the lane (``run_slice`` → ``lanes.pick_lane``).
#: Unset, ``run_slice`` reads ``code``. Every node whose session works
#: through the precis MCP carries another class: a generator its role (it
#: mints its children), the brief-writer :data:`BRIEF_TASK_CLASS` (posts
#: the ``brief:`` comment), a pcb fix :data:`PCB_TASK_CLASS` (drives the pcb
#: kind). Found 2026-11-04 as the codex lane went on (td562568): unclassed,
#: a triage would have launched on codex and ended with nothing minted.
#: The validate :data:`VALIDATE_TASK_CLASS` and the reviewer
#: :data:`REVIEW_TASK_CLASS` are classes the MCP-less lanes also fit
#: (td564019 cut 1): the gripe report is inlined in the validate body, the
#: reviewed land in the reviewer's brief, and each verdict rides the final
#: message (``validated:``, ``handoff: review: …``).
BRIEF_TASK_CLASS = "brief"
VALIDATE_TASK_CLASS = "validate"
PCB_TASK_CLASS = "pcb"


def _task(op: Any = "run_slice", *, task_class: str | None = None) -> dict[str, Any]:
    meta: dict[str, Any] = {"op": op}
    if task_class is not None:
        meta["task_class"] = task_class
    return meta


def _generate(role: str, source: str) -> dict[str, Any]:
    return {"generate": {"role": role, "from": source}, "task_class": role}


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


#: Longest gripe text a direct build's body embeds (the lane may have no
#: MCP, so the report rides the todo body; a longer one is cut with a note).
_DIRECT_REPORT_CAP = 12_000


def _gripe_report(store: Store, gid: int, handle: str, title: str) -> str:
    """The gripe's chunks (body and comments, in order) verbatim at mint
    time, headed by its handle and title: what a lane without the precis
    MCP gets instead of ``get(kind='gripe')``."""
    parts = [str(c.text) for c in store.chunks.list_chunks_for_ref(gid) if c.text]
    text = "\n\n".join(parts).strip()
    if len(text) > _DIRECT_REPORT_CAP:
        text = (
            text[:_DIRECT_REPORT_CAP]
            + "\n\n[report cut here at mint; the rest is on the gripe]"
        )
    return f"## {handle}: {title}\n\n{text}" if text else f"## {handle}: {title}"


def _direct_build_body(handle: str, report: str) -> str:
    return (
        f"Fix {handle} directly: no triage and no brief ran, and this lane may "
        "have no precis MCP, so the report is below verbatim as of mint. Find "
        "the code it names, commit the fix with its test on this branch, and "
        "touch nothing under the lane's deny paths. The land (land_slice, "
        "`scripts/ship --quick`) squash-merges onto main and writes "
        f"'resolved: <sha>' + verify on {handle}; a reviewer reads the land "
        "afterwards from another lane.\n\n" + report
    )


def mint_gripe_fix_direct(
    store: Store,
    parent_id: int | None = None,
    *,
    gripe_id: Any,
    lane: str | None = None,
) -> PlanMint:
    """:func:`mint_gripe_fix` with ``direct=True``: the ``gripe-fix-direct``
    template."""
    return mint_gripe_fix(store, parent_id, gripe_id=gripe_id, direct=True, lane=lane)


def mint_gripe_fix(
    store: Store,
    parent_id: int | None = None,
    *,
    gripe_id: Any,
    direct: bool = False,
    lane: str | None = None,
) -> PlanMint:
    """triage -> brief -> build -> gate sha-in-prod -> validate, under a phase
    root that is ``related-to`` the gripe (the gripe keeps the ticket state).

    A reviewer generator over the build's land is blocked-by the build only:
    the review is non-blocking, so the gate and the validate do not wait on
    it (dark-factory.md § "Branches").

    ``direct`` (the ``gripe-fix-direct`` template, td564019, Reto 2026-11-04:
    "the codex chain should run indep of claude") drops triage and brief:
    the build is the first node, its body carries the gripe's text verbatim
    (the lane may have no MCP), and no Claude step stands between the mint
    and a vendor-lane build. Review and validate still follow the land.
    ``lane`` pins the build's lane (``meta.op.params.lane``); unset, the
    class pick applies (``code`` -> the free vendor lanes first)."""
    gid = _gripe_id(gripe_id)
    gripe = store.get_ref(kind="gripe", id=gid)
    if gripe is None:
        raise BadInput(
            f"gripe {gid} not found",
            next="search(kind='gripe', q='...') to find the gripe id",
        )
    handle = handle_registry.format_handle("gripe", gid)
    title = str(gripe.title).strip()
    label = f"{handle}: {title[:80]}"
    build_op: Any = "run_slice"
    if lane:
        build_op = {"type": "run_slice", "params": {"lane": str(lane)}}

    report = _gripe_report(store, gid, handle, title)

    def build(m: _Minter) -> None:
        if direct:
            _build_direct(m)
            return
        _build_staged(m)

    def _build_direct(m: _Minter) -> None:
        m.node("root", f"Fix {label}", _phase())
        m.node(
            "build",
            f"Build the fix for {handle}",
            _task(build_op),
            body=_direct_build_body(handle, report),
        )
        _tail(m)

    def _tail(m: _Minter) -> None:
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
            _task(task_class=VALIDATE_TASK_CLASS),
            body=(
                "Re-run the repro or test the gripe names; an LLM read of the "
                "report against the landed change otherwise. The fix holds? "
                "Commit nothing and make `validated: <why>` the first line of "
                "your final message. It does not? Commit the correction on "
                "the branch and hand off as a build would. This lane may have "
                "no precis MCP: the report is below verbatim as of mint.\n\n" + report
            ),
            blocked_by=("gate",),
        )

    def _build_staged(m: _Minter) -> None:
        m.node("root", f"Fix {label}", _phase())
        m.node(
            "triage",
            f"Triage {handle}",
            _generate("triage", handle),
            body=(
                f"Read {handle}: pick the task class and the builder model — "
                "pin it on each `run_slice` child you mint as `meta.op` "
                "(`{'type': 'run_slice', 'params': {'model': '<id>', "
                "'effort': '<low|medium|high>'}}`, `<id>` from the ids the "
                "lane accepts); a model named only in the child's prose is "
                "lost at dispatch. Mint an ask gate when the report is "
                "unusable. Already resolved or closed? Mint nothing and "
                "make `moot: <why>` the first line of your final message: "
                "the plan closes on it."
            ),
        )
        m.node(
            "brief",
            f"Write the builder brief for {handle}",
            _task(task_class=BRIEF_TASK_CLASS),
            body=(
                f"A frontier model writes the builder's brief chunk for "
                f"{handle}; it names each step in the summary-title house "
                "style (`<object> · <behaviour> · <role>`, middot-separated, "
                "telegraphic). Post it as a `brief:` comment on the build "
                "node; a spec test may ride the branch. Nothing committed? "
                "End with `no-op: brief posted on td<build>` as the first "
                "line of your final message (gr561132)."
            ),
            blocked_by=("triage",),
        )
        m.node(
            "build",
            f"Build the fix for {handle}",
            _task(build_op),
            body=(
                f"run_slice on the lane triage picked, in its own worktree on "
                f"`slice/<id>`. Then the land (land_slice, `scripts/ship "
                "--quick`): rebase on main, lint (fix if red), squash to one "
                "commit, rebase again, squash-merge onto main, delete the "
                f"branch and worktree; it writes 'resolved: <sha>' + verify on "
                f"{handle}."
            ),
            blocked_by=("brief",),
        )
        _tail(m)

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
                "Mint one task per slice, each with a declared file set, "
                "titled in the summary-title house style (`<object> · "
                "<behaviour> · <role>`, middot-separated, telegraphic); "
                "sequence overlapping file sets with blocked-by links — "
                "order lives in the links, never in a title or body line."
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
    mints triage + fixer + re-cut + poll while red (td543733, td543855).

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


# -- pr-intake ---------------------------------------------------------------


#: The comment an ``analyst`` generator posts (``plan_dispatch`` closes the
#: node once it is there).
ANALYSIS_PREFIX = "analysis:"
#: Decision options per class; ``pr_action`` parses the answer against them.
DEPENDABOT_OPTIONS = ["land", "ignore this major", "hold"]
EXTERNAL_OPTIONS = [
    "land as is",
    "land after fixes: <list>",
    "request changes on the PR",
    "close",
]
HARNESS_OPTIONS = ["land via /go", "request changes on the PR", "close"]
_PR_RE = re.compile(r"(?:pr[:#\s]*|#)?(\d+)", re.IGNORECASE)


def _pr_number(raw: Any) -> int:
    m = _PR_RE.fullmatch(str(raw).strip())
    if m is None or int(m.group(1)) <= 0:
        raise BadInput(
            f"pr={raw!r} is not a pull request number",
            next="args={'template': 'pr-intake', 'pr': 73}",
        )
    return int(m.group(1))


def _pr_facts(store: Store, number: int) -> pr_class.PrFacts:
    repo = pr_class.resolve_repo(store)
    if not repo:
        raise BadInput(
            "no GitHub owner/repo to read the pull request from",
            next="set the release.repo setting to 'owner/repo'",
        )
    try:
        return pr_class.fetch_facts(repo, number)
    except pr_class.GitHubUnreadable as exc:
        raise Upstream(
            f"GitHub did not return PR #{number} of {repo}: {exc}",
            next="retry in a few minutes (rate limit or network); a 404 means "
            "the PR number or release.repo is wrong",
        ) from exc


def _pr_label(f: pr_class.PrFacts) -> str:
    return f"PR #{f.number}: {' '.join(f.title.split())[:80]}"


def _land_op(params: dict[str, Any]) -> dict[str, Any]:
    return {"op": {"type": "land_slice", "params": params}}


def _stamp_land(m: _Minter, land_id: int, number: int) -> None:
    """The land node's op names its own todo (known only after the insert)."""
    params = {"todo_id": land_id, "pr": number, "checked": True}
    m.store.stamp_ref_meta(land_id, _land_op(params), conn=m.conn)


def _pr_reviewer_brief(target_id: int, pr: dict[str, Any]) -> str:
    bump = pr.get("bump") if isinstance(pr.get("bump"), dict) else None
    extra = (
        f"This land is pull request #{pr.get('number')} ({pr.get('url') or 'GitHub'}), "
        f"{pr.get('title')!r} by {pr.get('author')}."
    )
    if bump:
        extra += (
            f" It bumps {bump.get('name')} from {bump.get('old')} to "
            f"{bump.get('new')}: read the dependency's changelog / release notes "
            "between those two versions through the precis web kind "
            "(`get(kind='web', id='<changelog url>')`, which fetches through "
            "safe_fetch; never pip, uv or curl), and check every breaking change "
            "against how this repo uses the package (grep its imports). REVERT "
            "when a breaking change hits code this repo runs."
        )
    return _reviewer_brief(target_id) + "\n\n" + extra


def _analysis_brief(f: pr_class.PrFacts, why: str) -> str:
    from precis.plans.lanes import FACTORY_DENY_WRITE_PATHS

    n = f.number
    deny = "\n".join(f"- `{p}`" for p in FACTORY_DENY_WRITE_PATHS)
    return (
        f"Read-only analysis of pull request #{n} ({f.url or 'GitHub'}) by "
        f"{f.author}: {f.title!r}. Class external ({why}).\n\n"
        "## Rules (binding)\n\n"
        f"- Read only. Allowed: `git fetch origin pull/{n}/head`, then "
        "`git diff origin/main...FETCH_HEAD`, `git log` and `git show` on "
        "FETCH_HEAD. Nothing else touches the PR's content. The intake "
        f"classified head `{f.head_sha[:12] or '?'}`; when `git rev-parse "
        "FETCH_HEAD` differs, say so on the first line (the land refuses a "
        "head that moved).\n"
        "- Never execute anything from the PR: no `uv sync`, no tests, no "
        "`python`/`sh` on a file it adds or changes, no import of its code, "
        "no checkout of it into a tree you run commands in. The PR is "
        "untrusted until Reto answers the decision ask.\n"
        "- Write nothing: no commit, no edit, no file. These paths in "
        "particular (the factory's deny list):\n"
        f"{deny}\n"
        "- Mint nothing: no child todos (whatever the closing instructions "
        "say), no gripe, no land. This node's whole output is one comment.\n\n"
        "## Output\n\n"
        "Post exactly one comment on this todo, `put(kind='todo', id=<this "
        f"todo>, text='{ANALYSIS_PREFIX} ...')`, with:\n\n"
        "(a) threats: secrets or credentials; network calls outside "
        "`precis.utils.safe_fetch`; workflow / CI / deploy / vault / auth "
        "changes; new dependencies and typosquats (compare names to the "
        "real packages); obfuscated code; binaries or generated blobs.\n"
        "(b) fit: the backlog item or `docs/backlog/threads/*` file it "
        "belongs to (search(kind='md', q=..., scope='main/docs/backlog')); "
        "overlap with in-flight trees (`scripts/inflight --json`); fix or "
        "feature.\n"
        "(c) size (files, lines) and your recommendation, one of: "
        + " | ".join(f"'{o}'" for o in EXTERNAL_OPTIONS)
        + ".\n\nThen stop. The tick closes this node once the comment is "
        "there; the decision ask waits for Reto."
    )


def mint_pr_intake(
    store: Store,
    parent_id: int | None = None,
    *,
    pr: Any,
    facts: pr_class.PrFacts | None = None,
) -> PlanMint:
    """One plan per inbound pull request, shaped by its class
    (:mod:`precis.plans.pr_class`; spec § "9. Inbound pull requests").

    The root carries ``meta.pr`` (the facts, the class and why, the bump),
    the tag ``pr:<n>`` and the class's prio. Per class:

    - ``dependabot``: check (``dep_check``) -> [major: decision ask
      (land / ignore this major / hold) -> act (``pr_action``)] -> land
      (``land_slice`` on ``slice/pr-<n>``, ``--checked``) -> review.
    - ``factory``: land -> review.
    - ``harness``: decision ask (land via /go / request changes / close)
      -> act.
    - ``external``: analysis (``analyst`` generator, read-only) -> decision
      ask (``approval`` for a fix, ``design-verdict`` for a feature) -> act
      -> land -> review.

    ``facts`` (``pr_poll`` passes them) skips the GitHub read. The root is
    also tagged ``pr-head:<sha12>`` (the head it was minted on). Refuses a
    PR that has an open intake plan, or a closed one on the same head
    (:func:`precis.plans.pr_class.needs_intake`); a head that moved after the
    plan closed gets a new plan."""
    number = _pr_number(pr)
    f = facts if facts is not None else _pr_facts(store, number)
    if not pr_class.needs_intake(store, number, f.head_sha):
        existing = pr_class.existing_intake(store, number)
        raise BadInput(
            f"PR #{number} already has an intake plan (td{existing})",
            next=f"get(kind='todo', id={existing})",
        )
    cls, why = pr_class.classify(f, pr_class.builders(store))
    meta_pr = f.to_meta(cls, why)
    label = _pr_label(f)

    def decision(
        m: _Minter,
        title: str,
        kind: str,
        options: list[str],
        blocked_by: tuple[str, ...],
    ) -> None:
        ask: dict[str, Any] = {
            "kind": kind,
            "options": options,
            "blocks": "nothing",
            "context": f"{label} ({cls}: {why})"[:600],
        }
        ask_id = m.node(
            "decision",
            title,
            {"gate": {"kind": "answer"}, "ask": ask},
            body=(
                f"{label} — {f.url or 'GitHub'}. Answer with put(kind='todo', "
                "id=<this>, text='answer: <one option>') and close this todo; "
                "pr_action acts on the answer (the PR is touched only after it)."
            ),
            blocked_by=blocked_by,
        )
        _tag(m, ask_id, "ask-user")
        act = m.node(
            "act",
            f"Act on the answer for PR #{number}",
            _task("pr_action"),
            body=(
                "pr_action reads the decision's answer: a land answer lets the "
                "land run; any other answer posts the PR comment or closes it "
                "(outward-facing, only now) and skips the rest of this plan."
            ),
            blocked_by=("decision",),
        )
        final = {**ask, "blocks": f"td{act}"}
        check_ask_meta({"ask": final})
        m.store.stamp_ref_meta(ask_id, {"ask": final}, conn=m.conn)

    def land_and_review(m: _Minter, blocked_by: tuple[str, ...]) -> None:
        land = m.node(
            "land",
            f"Land PR #{number}",
            _land_op({"pr": number, "checked": True}),
            body=(
                f"land_slice lands slice/pr-{number} (the PR head"
                + (", relocked by dep_check" if cls == pr_class.DEPENDABOT else "")
                + ") with scripts/ship --checked, then closes the PR with a "
                "comment naming the squash sha."
            ),
            blocked_by=blocked_by,
        )
        _stamp_land(m, land, number)
        m.node(
            "review",
            f"Review the PR #{number} land",
            _reviewer(land),
            body=_pr_reviewer_brief(land, meta_pr),
            blocked_by=("land",),
        )

    def build(m: _Minter) -> None:
        root = m.node("root", label, {**_phase(), "pr": meta_pr})
        _tag(m, root, f"pr:{number}")
        head = pr_class.head_tag(f.head_sha)
        if head:
            _tag(m, root, head)
        m.store.set_prio(root, pr_class.prio_of(cls, f), conn=m.conn)
        if cls == pr_class.DEPENDABOT:
            m.node(
                "check",
                f"Resolve PR #{number}'s lock under python 3.12 and 3.13",
                _task("dep_check"),
                body=(
                    f"dep_check fetches the PR head into slice/pr-{number}, "
                    "relocks when uv.lock is stale and resolves the lock for "
                    "python 3.12 and 3.13 (uv, --no-build: resolve only, the "
                    "dependency is never built, imported or run)."
                ),
            )
            gate: tuple[str, ...] = ("check",)
            if pr_class.is_major(f):
                bump = pr_class.bump_of(f.title)
                what = f"{bump.name} {bump.old} -> {bump.new}" if bump else f.title
                decision(
                    m,
                    f"Land the major bump {what}? (answer 'land', 'ignore this "
                    "major' or 'hold')",
                    "approval",
                    DEPENDABOT_OPTIONS,
                    ("check",),
                )
                gate = ("act",)
            land_and_review(m, gate)
        elif cls == pr_class.FACTORY:
            land_and_review(m, ())
        elif cls == pr_class.HARNESS:
            decision(
                m,
                f"PR #{number} touches the landing harness: land it by hand with "
                "/go, request changes, or close?",
                "approval",
                HARNESS_OPTIONS,
                (),
            )
        else:
            m.node(
                "analysis",
                f"Analyse PR #{number} (read-only)",
                {**_generate("analyst", f"pr:{number}"), "task_class": "review"},
                body=_analysis_brief(f, why),
            )
            decision(
                m,
                f"Land PR #{number}? Read the analysis comment first",
                pr_class.ask_kind(f),
                EXTERNAL_OPTIONS,
                ("analysis",),
            )
            land_and_review(m, ("act",))

    return _run(store, parent_id, build)


def mint_pr_fix(
    store: Store, plan_root: int, pr: dict[str, Any], fixes: str
) -> dict[str, int]:
    """The 'land after fixes' path (``pr_action``): one ``run_slice`` task
    that merges the PR head into its slice branch and fixes ``fixes``
    (``meta.pr_close`` makes its land close the PR), and a reviewer over
    it. The PR head the intake classified (``meta.pr.head_sha``) is pinned:
    the fix node's op params and brief carry it, and the brief merges that
    sha, never a bare ``FETCH_HEAD``. Returns ``{'fix': id, 'review': id}``."""
    n = int(pr["number"])
    sha = str(pr.get("head_sha") or "").strip().lower()
    pinned = bool(pr_class.SHA40_RE.fullmatch(sha))
    op: Any = (
        {"type": "run_slice", "params": {"head_sha": sha}} if pinned else "run_slice"
    )
    if pinned:
        merge = (
            f"`git fetch origin {sha}`; when that is refused, `git fetch origin "
            f"pull/{n}/head`. Then check `git rev-parse FETCH_HEAD` prints "
            f"`{sha}` — if it prints anything else the PR head moved since "
            "the decision: stop, merge nothing and say so in your final message "
            f"(a moved head needs a new intake). Then `git merge --no-edit {sha}` "
            "(that sha, never a bare FETCH_HEAD)"
        )
    else:
        merge = (
            f"`git fetch origin pull/{n}/head` then `git merge --no-edit "
            "FETCH_HEAD` (the intake recorded no head sha to pin)"
        )

    def build(m: _Minter) -> None:
        fix = m.node(
            "fix",
            f"Fix PR #{n} before it lands",
            {**_task(op), "pr_close": n},
            body=(
                f"Pull request #{n} ({pr.get('title')!r} by {pr.get('author')}) "
                "was approved to land after fixes. First bring it onto your "
                f"branch: {merge}. Then make these fixes:\n\n"
                f"{fixes.strip()}\n\nThe lander lands your branch (the PR "
                f"included) and closes PR #{n} with the squash sha."
            ),
        )
        m.node(
            "review",
            f"Review the PR #{n} land (with fixes)",
            _reviewer(fix),
            body=_pr_reviewer_brief(fix, pr),
            blocked_by=("fix",),
        )

    return _run_body(store, plan_root, plan_root, build).ids


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


# -- route-loop --------------------------------------------------------------


#: Iterations a ``route-loop`` runs before it parks: each one measures the
#: example boards, builds the next fix slices and reviews their lands.
_ROUTE_LOOP_MAX = 6
#: The loop body a ``route-loop`` re-mints (a key of :data:`BODIES`).
ROUTE_FIX_BODY = "route-fix"
#: What every ``route-fix`` iteration measures: the examples route gate,
#: every row (``scripts/test`` alone skips the ``slow`` nano rows).
ROUTE_GATE_TEST = "tests/test_pcb_examples_route.py"
ROUTE_GATE_CMD = f'scripts/test {ROUTE_GATE_TEST} -m "slow or not slow"'
#: The comment a route-fix slicer posts on the loop todo once every board
#: routes fully at DRC 0; the loop's ``answer`` until reads it there.
ROUTE_ANSWER = "answer: all example boards route fully at DRC 0 at <sha>"
_ROUTE_NO_OP = "no-op: all example boards route fully at DRC 0"
_TODO_ID_RE = re.compile(r"(?:td|todo:)?(\d+)", re.IGNORECASE)


def _todo_ids(store: Store, raw: Any, *, arg: str) -> list[int]:
    """Live todo ids named by ``raw`` (``'td1, td2'``, a list, or one id)."""
    if raw in (None, ""):
        return []
    items = list(raw) if isinstance(raw, (list, tuple)) else str(raw).split(",")
    out: list[int] = []
    for item in items:
        spelled = str(item).strip()
        m = _TODO_ID_RE.fullmatch(spelled)
        if m is None:
            raise BadInput(
                f"{arg}={raw!r}: {spelled!r} is not a todo id",
                next=f"args={{'template': 'route-loop', '{arg}': 'td123, td456'}}",
            )
        tid = int(m.group(1))
        if store.get_ref(kind="todo", id=tid) is None:
            raise BadInput(
                f"{arg}={raw!r}: todo {tid} not found",
                next=f"name live todo ids in {arg}",
            )
        out.append(tid)
    return out


def _iterations(raw: Any) -> int:
    if raw in (None, ""):
        return _ROUTE_LOOP_MAX
    try:
        cap = int(raw)
    except (TypeError, ValueError):
        cap = 0
    if cap <= 0:
        raise BadInput(
            f"iterations={raw!r} must be an integer > 0",
            next="args={'template': 'route-loop', 'iterations': 6}",
        )
    return cap


def mint_route_loop(
    store: Store,
    parent_id: int | None = None,
    *,
    iterations: Any = None,
    after: Any = None,
    context: Any = None,
) -> PlanMint:
    """phase -> loop until an ``answer:`` is the newest comment on the loop
    todo (body ``route-fix``, max ``iterations``, default 6).

    Every iteration (:func:`mint_route_fix`) measures the example boards
    with :data:`ROUTE_GATE_CMD`, mints the next fix slices from the failing
    nets and reviews their lands; its slicer posts :data:`ROUTE_ANSWER` on
    the loop todo once every board routes fully at DRC 0, and the loop's
    ``answer`` until closes it (``gates.evaluate`` reads a loop's until on
    the loop todo itself).

    No first iteration is minted here: the tick mints iteration 1 once the
    loop is unblocked and childless, so a loop minted ahead of its
    prerequisites does not fire early (me570353: a premature job eats a
    retry). ``after`` names the todos the loop waits for (``blocked-by``
    links, ``'td1, td2'``). ``context`` becomes the loop todo's body and
    rides verbatim into every iteration's slicer brief: the design
    pointers, lane pins and rulings a fresh session has no memory of."""
    cap = _iterations(iterations)
    waits = _todo_ids(store, after, arg="after")
    ctx = str(context or "").strip()

    def build(m: _Minter) -> None:
        m.node("root", "Route every example board fully at DRC 0", _phase())
        loop = m.node(
            "loop",
            "Example boards · measured, sliced, built and reviewed until every "
            "board routes fully at DRC 0 · loop",
            {
                "loop": {
                    "max": cap,
                    "until": {"kind": "answer"},
                    "body": ROUTE_FIX_BODY,
                }
            },
            body=ctx or None,
        )
        for tid in waits:
            m.store.add_link(
                src_ref_id=loop, dst_ref_id=tid, relation="blocked-by", conn=m.conn
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
            optional=("lane",),
        ),
        Template(
            "gripe-fix-direct",
            ("gripe_id",),
            "build (the gripe's text in the body; no triage, no brief, no "
            "Claude step before a vendor lane) -> sha-in-prod gate -> validate "
            "(+ a non-blocking review of the land); 'lane' pins the build",
            mint_gripe_fix_direct,
            optional=("lane",),
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
            "pr-intake",
            ("pr",),
            "one inbound pull request by class: dependabot (dep_check -> "
            "[major ask] -> land --checked -> review), factory (land -> review), "
            "harness (ask for /go), external (read-only analysis -> ask -> land "
            "-> review)",
            mint_pr_intake,
        ),
        Template(
            "pcb-pipeline",
            ("board",),
            "place+route -> drc/review loop until the DRC is clean",
            mint_pcb_pipeline,
        ),
        Template(
            "route-loop",
            (),
            "measure the example boards -> slicer mints the next fix slices -> "
            "review their lands; loop until the slicer answers that every board "
            "routes fully at DRC 0 (max 'iterations'; waits for 'after'; "
            "'context' rides into every slicer brief)",
            mint_route_loop,
            optional=("iterations", "after", "context"),
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
    """triage (generator over gripes tagged ``plan:<root>``) -> fix -> re-cut
    (``release_cut``) -> poll (``ci_verdict_poll``), td543855.

    The re-cut is the mechanical op the release-pass ``cut`` is: it records
    the new cut on the plan root (:func:`precis.plans.release.record_cut`
    overwrites ``meta.release`` and rewrites the open ``tests-green`` gate,
    the loop's ``until`` and the approval options to the new range / r<N>).
    The poll reads that new ``meta.release``, so the loop -- which waits for
    every child to close -- re-evaluates ``until`` on a recorded verdict for
    the new range: red mints the next iteration, green closes the loop while
    the gate opens and frees the approval ask."""
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
            _task("release_cut"),
            body=(
                "release_cut runs scripts/release --cut-only on main with the fixes "
                "in, then records the new cut on the plan root (meta.release) and "
                "the new range on the tests-green gate and this loop's until."
            ),
            blocked_by=("fix",),
        )
        m.node(
            "poll",
            f"Record the CI verdict on the re-cut{suffix}",
            {**_task("ci_verdict_poll"), "retries": _POLL_RETRIES},
            body=(
                "ci_verdict_poll reads check.yml's run on the re-cut's sha from "
                "the GitHub API and records it in release.ci_verdicts."
            ),
            blocked_by=("recut",),
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
            _task(task_class=PCB_TASK_CLASS),
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


def _todo_body(store: Store, todo_id: int) -> str:
    """The todo's details body (its ``todo_body`` chunks), or ``''``."""
    return "\n".join(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == TODO_BODY_KIND and c.text
    ).strip()


def _route_measure_body(
    loop_id: int, iteration: int | None, cap: Any, context: str
) -> str:
    """The route-fix slicer's brief: measure, then answer or slice."""
    which = f"iteration {iteration} of {cap}" if iteration else "an iteration"
    text = (
        "Measure the example boards on origin/main and mint the next router fix "
        f"slices: {which} of the loop td{loop_id}, which closes when an "
        f"`answer:` comment is the newest on td{loop_id}.\n\n"
        f"Step 0. Read the newest comment on td{loop_id}. If it starts `answer:` "
        "(every board already routes fully at DRC 0), run nothing and make "
        f"`{_ROUTE_NO_OP}` the first line of your final message.\n\n"
        f"Step 1, measure. Run `{ROUTE_GATE_CMD}` (the nano rows take about 30 "
        "minutes each at effort 20000; the file runs under two hours; your wall "
        "cap is four). Record per board: routable, routed, failed, drc_errors, "
        "seconds and the cause (the assertion text carries failed= and "
        "unrouted= net names; a LocalError names the corridor). A strict XPASS "
        "means that board is complete and its xfail row is stale. Post the "
        f"table as one `measure: <origin/main sha>` comment on td{loop_id}, one "
        "line per board.\n\n"
        "Step 2. If every board is complete and no xfail row is left: post "
        f"`{ROUTE_ANSWER}` on td{loop_id} (it must be the newest comment there) "
        f"and make `{_ROUTE_NO_OP}` the first line of your final message.\n\n"
        "Step 3. Otherwise mint child todos under this todo (args parent_id = "
        "this todo's id), each a code slice a builder in a fresh worktree "
        "executes from the body alone. Group the failing nets by cause (pins "
        "walled off at the pad; copper withdrawn by the short or connectivity "
        "guard; CorridorOverflow before routing; via sites refused; unresolved "
        "crossings) and read each cause's code path before writing: "
        "src/precis/pcb/topoplace/wires.py (build_wires, _Corridor, "
        "_ViaPlanter, _Shove), realise.py (_route_pass, _guarded_branches, "
        "plan_route, assign_layers), shove.py, mesh.py, crossings.py, "
        "copper_index.py. Each child body names: the goal; the boards and nets "
        "it targets with today's numbers; the mechanism to change with every "
        "entry point as module::function; the file set; acceptance in one "
        f"place: a unit test for the mechanism; `{ROUTE_GATE_CMD}` with no "
        "board worse and the targeted rows improved by a stated number or "
        "flipped from xfail to a plain pass in the same commit (docstring table "
        "updated); `scripts/test --typecheck` clean; "
        "tests/test_pcb_topoplace_invariant.py left alone (its ledger rows skip "
        'on the slice host); encoding="utf-8" on text IO; one commit '
        "`type(scope): what (td<child>)` via `git commit -F`. A stale xfail row "
        "is flipped by the fix child that completes the board; a board that "
        "XPASSes with nothing left to fix gets one child that flips its row "
        "and updates the table. Sequence children whose file sets overlap with "
        "blocked-by links (edges, never prose). Mint at most four children. "
        "Set a child's meta.op only as the design context below says (the "
        "closing section's model pin is for children that need the precis MCP, "
        "and none here does).\n\n"
        "Commit nothing yourself."
    )
    if context:
        text += f"\n\nDesign context (the body of td{loop_id}, verbatim):\n\n{context}"
    return text


def mint_route_fix(
    store: Store, loop_todo_id: int, *, iteration: int | None = None, **_ctx: Any
) -> PlanMint:
    """measure (slicer generator: runs the examples gate, posts the
    ``measure:`` table on the loop todo, mints the next fix slices or the
    ``answer:`` that closes the loop) -> review (reviewer generator over the
    slicer's children). The loop todo's body is the design context every
    iteration's slicer gets verbatim (:func:`mint_route_loop`)."""
    loop, root = _loop_context(store, loop_todo_id)
    suffix = _iteration_suffix(iteration)
    context = _todo_body(store, loop_todo_id)

    def build(m: _Minter) -> None:
        measure = m.node(
            "measure",
            "Example boards · measured on origin/main, stale rows flipped, next "
            f"fix slices minted{suffix} · slicer",
            _generate("slicer", ROUTE_GATE_TEST),
            body=_route_measure_body(loop_todo_id, iteration, loop.get("max"), context),
        )
        m.node(
            "review",
            "Fix slices · landed diffs read against their briefs, one verdict "
            f"posted{suffix} · reviewer",
            _reviewer(measure),
            body=(
                _reviewer_brief(measure, slices=True)
                + f"\n\nNo child todo under td{measure} (the boards were complete, "
                "or nothing has landed)? Post `review: LAND`."
            ),
            blocked_by=("measure",),
        )

    return _run_body(store, loop_todo_id, root, build)


#: Loop body name -> minter ``(store, loop_todo_id, **ctx) -> PlanMint``. The
#: tick passes ``iteration=<n>``; a body ignores context it does not use.
BODIES: dict[str, Callable[..., PlanMint]] = {
    "release-fix": mint_release_fix,
    "pcb-fix": mint_pcb_fix,
    ROUTE_FIX_BODY: mint_route_fix,
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
