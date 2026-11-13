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
from precis.plans.nodes import finish_mint, validate_plan_meta
from precis.store import Store, Tag
from precis.store.types import TODO_BODY_KIND, ChunkInsert
from precis.todo.guards import check_ask_meta
from precis.utils import dispatch_score as _dscore
from precis.utils import handle_registry

#: ``meta.gate.args.sha`` of a ``sha-in-prod`` gate minted before the build
#: that fills it has landed (valid hex, matches no commit).
PENDING_SHA = "0000000"
GATE_SHA_NOTE = (
    "The sha in meta.gate is a placeholder ('0000000'): the build task's land "
    "writes the real sha here once it is known. Until then this gate stays false."
)
#: The ``release-fix`` loop's one iteration cap (me573231: two red fix
#: rounds, then a human call). Not stamped on the loop (T4 dropped the
#: stamped max): ``plan_dispatch._RELEASE_FIX_AUTO_CAP`` reads this.
#: release-train.md says progress is the limit;
#: the cap stays until T5's no-progress alarm replaces it.
RELEASE_FIX_CAP = 2
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
        for tid in m.ids.values():
            finish_mint(store, tid, conn=conn)
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

#: ``meta.preland_review`` on a reviewer generator: the review GATES the
#: land (gr570506, Reto 2026-11-12: "the squash commit to main is the LAST
#: step"). ``run_slice`` pushes the build's branch to origin, stamps the
#: held ``land_slice`` params as :data:`PENDING_LAND_KEY` on the build
#: todo and closes it — the tick releases this reviewer, mints the held
#: land on a LAND verdict, and sends FIX/REVERT back to the builder.
PRELAND_REVIEW_KEY = "preland_review"
#: ``meta.pending_land`` on a build todo whose land waits on a pre-land
#: reviewer: the params ``run_slice._mint_land`` would have minted
#: ``land_slice`` with (todo_id, worktree, branch, lane?, checked?,
#: target_node?). ``plan_dispatch`` clears the stamp when the verdict
#: disposes of it (LAND mints, FIX/REVERT reopens the builder).
PENDING_LAND_KEY = "pending_land"

#: The verdict lines a reviewer posts; ``plan_dispatch`` parses them.
REVIEW_GRAMMAR = (
    "`review: LAND` (clean) | `review: FIX — <the list of findings>` | "
    "`review: REVERT — <why>`"
)

#: The marker a read-only reviewer ends its verdict line with (gr573555):
#: it runs no tests, so the verdict carries this, not a test claim.
#: ``plan_dispatch`` strips it where it ends a line — accepted, never
#: required.
REVIEW_READONLY_MARKER = "(read-only, tests not run)"


def _reviewer(target_id: int, *, preland: bool = False) -> dict[str, Any]:
    """A reviewer generator over ``td<target_id>``'s diff — of its landed
    squash by default; gated pre-land when ``preland`` (:data:`PRELAND_REVIEW_KEY`,
    gr570506: the squash onto main is the LAST step)."""
    meta = {**_generate("reviewer", f"td{target_id}"), "task_class": REVIEW_TASK_CLASS}
    if preland:
        meta[PRELAND_REVIEW_KEY] = True
    return meta


def _reviewer_brief(
    target_id: int, *, slices: bool = False, preland: bool = False
) -> str:
    """The reviewer's brief (the node body ``run_slice`` puts in the session
    brief): read the diff against the brief, post one verdict — the landed
    squash by default; the pushed branch when ``preland`` (gr570506)."""
    if preland:
        intro = "Review BEFORE land, from another lane than the builder's."
        what = (
            f"td{target_id}'s branch is pushed as `origin/slice/{target_id}`: "
            f"`git fetch -q origin`, then `git diff origin/main...origin/slice/"
            f"{target_id}` against td{target_id}'s brief chunk "
            f"(`get(kind='todo', id={target_id})`)."
        )
    elif slices:
        intro = "Review after land, from another lane than the builder's."
        what = (
            f"Each child todo of td{target_id} is a slice that landed on main: "
            "its `landed: <sha>` comment names the squash commit. For each, "
            "read `git show <sha>` against that child's brief chunk "
            "(`get(kind='todo', id=<child>)`)."
        )
    else:
        intro = "Review after land, from another lane than the builder's."
        what = (
            f"td{target_id} landed on main: its `landed: <sha>` comment names "
            "the squash commit. Read `git show <sha>` against td"
            f"{target_id}'s brief chunk (`get(kind='todo', id={target_id})`)."
        )
    return (
        f"{intro} {what} Judge whether the diff does what the brief asked, "
        "and nothing it should not.\n\n"
        + (_PRELAND_REVIEWER_BRIEF_TAIL if preland else _REVIEWER_BRIEF_TAIL)
    )


#: The verdict/mint rules every reviewer brief ends with.
_REVIEWER_BRIEF_TAIL = (
    "Post exactly one comment on this todo, "
    "`put(kind='todo', id=<this todo>, text=...)`, as one of: "
    f"{REVIEW_GRAMMAR}. The text after the dash is what the fix or the "
    "revert slice is built from: name files and the concrete problem.\n\n"
    "You are read-only: run no tests, no typecheck, no lint and no "
    "`uv` command; read the squash diff and the files. End your "
    f"verdict line with `{REVIEW_READONLY_MARKER}`.\n\n"
    "Mint nothing yourself: no child todos, no fix, no revert, no commit, "
    "no land, and do not tag this todo done. The plan tick reads the "
    "verdict: LAND closes this review, FIX mints the fix slice, REVERT "
    "mints the revert slice and requeues the build."
)


#: The pre-land variant (gr570506): the squash has not landed — LAND lets
#: the held ``land_slice`` mint, FIX/REVERT send the branch back to the
#: builder (no revert task: nothing is on main to revert).
_PRELAND_REVIEWER_BRIEF_TAIL = (
    "Post exactly one comment on this todo, "
    "`put(kind='todo', id=<this todo>, text=...)`, as one of: "
    f"{REVIEW_GRAMMAR}. The text after the dash is what the rebuild is "
    "briefed with: name files and the concrete problem.\n\n"
    "You are read-only: run no tests, no typecheck, no lint and no "
    "`uv` command; read the branch diff and the files. End your "
    f"verdict line with `{REVIEW_READONLY_MARKER}`.\n\n"
    "Mint nothing yourself: no child todos, no fix, no revert, no commit, "
    "no land, and do not tag this todo done. The plan tick reads the "
    "verdict: LAND mints the held land_slice (the squash onto main is "
    "the last step), FIX or REVERT sends the branch back to the builder "
    "with your findings as its handoff — nothing is on main to revert.\n\n"
    "Reject (FIX) a diff that changes behavior or tests without the "
    "matching documentation (docstrings, comments, skills, docs) updated "
    "in the same diff, and check one hop out: read the callers and "
    "callees of every changed function and reject a change that leaves "
    "them reading wrong or broken."
)


def _sha_reviewer_brief(sha12: str) -> str:
    """The sha-keyed reviewer's brief: the commit IS the brief — no live
    plan root claims it (gr597625)."""
    return (
        "Review after land, from another lane than the builder's. Commit "
        f"{sha12} is on main — an engine land no live plan root claims "
        "(no `land_slice` job or subject handle links it, or the linked "
        "todo's root is dead). Read `git show " + sha12 + "` and "
        "judge whether the diff is fit for main, and does nothing it "
        "should not.\n\n" + _REVIEWER_BRIEF_TAIL
    )


def mint_plan_task(
    store: Store, plan_root: int, title: str, body: str, *, op: Any = "run_slice"
) -> int:
    """Mint one task todo directly under ``plan_root``, tagged
    ``plan:<plan_root>`` (the tick's fix / revert slices). Returns its id."""

    def build(m: _Minter) -> None:
        m.node("task", title, _task(op), body=body)

    return _run_body(store, plan_root, plan_root, build).ids["task"]


def mint_merge_back_resolver(
    store: Store, plan_root: int, rn: str, body: str
) -> tuple[int, int]:
    """``(resolver, ours)`` — the two nodes a conflicting merge-back
    (release-train T3, :mod:`precis.workers.job_types.release_merge_back`)
    mints under the release pass's root, in one transaction: the
    resolver, a normal ``run_slice`` slice off main tagged
    ``meta.merge_back_resolver = rN`` (the key the caller dedupes on),
    and a second ``merge_back`` node blocked-by it whose
    ``strategy="ours"`` merge records the release head as an ancestor
    of main once the resolver's squash carried the content."""

    def build(m: _Minter) -> None:
        rid = m.node(
            "resolve",
            f"Resolve the merge of release/{rn} back into main",
            {**_task(), "merge_back_resolver": rn},
            body=body,
        )
        op: dict[str, Any] = {
            "type": "release_confirm",
            "params": {"merge_back": True, "strategy": "ours", "resolver": rid},
        }
        mid = m.node(
            "merge_back",
            f"Record release/{rn} as merged into main",
            {"op": op},
            body=(
                f"release_confirm (merge_back mode, strategy=ours): once the "
                f"resolver td{rid} landed, `git merge --no-ff -s ours "
                f"origin/release/{rn}` off origin/main, message 'Merge "
                f"release/{rn} fix forward (resolved in <sha>)', pushed to "
                "main — the release head becomes an ancestor of main with "
                "main's tree unchanged."
            ),
            blocked_by=("resolve",),
        )
        op["params"]["todo_id"] = mid
        m.store.stamp_ref_meta(mid, {"op": op}, conn=m.conn)

    ids = _run_body(store, plan_root, plan_root, build).ids
    return ids["resolve"], ids["merge_back"]


def mint_reviewer(store: Store, plan_root: int, target_id: int) -> int:
    """Mint one reviewer node over the already-landed td<target_id>,
    directly under ``plan_root`` and tagged ``plan:<plan_root>`` — the
    same shape the templates wire ``blocked-by`` their build, edge
    included, pointed at the landed todo: it is done at mint so the
    review dispatches at once, and ``plan_dispatch._review_revert``
    reopens it behind the revert task, which holds the reviewer
    through the rebuild — without the edge a REVERT verdict would
    re-dispatch the review of the same land in a loop (td584381 S1b).
    Returns its id.

    ``release_poll.engine_review_blockers`` calls this when an engine
    land has no covering reviewer; the caller serializes its dedupe
    re-read and this mint under one advisory lock — inside
    ``store.atomic()`` the ``store.tx()`` here joins the caller's
    transaction as a savepoint. Hang the node only under a LIVE root:
    one open under a dead root is judged hopeless by the gate (and hung
    on the reviewed todo itself it would read as its own review
    target)."""

    def build(m: _Minter) -> None:
        rid = m.node(
            "review",
            f"Review the td{target_id} land",
            _reviewer(target_id),
            body=_reviewer_brief(target_id),
        )
        store.add_link(
            src_ref_id=rid,
            dst_ref_id=target_id,
            relation="blocked-by",
            conn=m.conn,
        )

    return _run_body(store, plan_root, plan_root, build).ids["review"]


def mint_sha_reviewer(store: Store, sha12: str) -> int:
    """Mint the sha-keyed reviewer over a hand-landed engine commit —
    nothing links it, or the linked todo has no live plan root to hang
    a td-keyed reviewer under (gr597625, td599062): a
    phase root plus one reviewer child whose ``generate.from`` is
    ``sha:<sha12>`` — the key the engine-review gate dedupes and covers
    on (a sha key covers every unit of its commit). The phase root keeps
    the FIX/REVERT task
    ``plan_dispatch._step_reviews`` mints under it reachable while it
    runs, then closes once every child closed.
    ``release_poll._mint_sha_reviewer`` calls this inside its dedupe-lock
    transaction (``store.tx()`` joins it as a savepoint); it mints nothing
    deferred, so there is no post-commit step. Returns the reviewer's id."""
    sha12 = sha12[:12]

    def build(m: _Minter) -> None:
        m.node("root", f"Engine review of the {sha12} hand land", _phase())
        m.node(
            "review",
            f"Review the {sha12} engine land",
            {
                **_generate("reviewer", f"sha:{sha12}"),
                "task_class": REVIEW_TASK_CLASS,
            },
            body=_sha_reviewer_brief(sha12),
        )

    with store.tx() as conn:
        m = _Minter(store, conn, None)
        build(m)
        for tid in m.ids.values():
            finish_mint(store, tid, conn=conn)
    return m.ids["review"]


# -- gripe-fix ---------------------------------------------------------------


#: The migration-DDL paragraph a migration-bearing slice's brief carries
#: verbatim (gr570072): a builder lane cannot read the skills or
#: ``scripts/ship`` to learn the squawk rules, and the first todo-driven
#: factory land went red on exactly these (``CREATE TABLE`` without
#: ``IF NOT EXISTS``, a ``smallint`` column). ``run_slice.render_brief``
#: appends it whenever the slice body names a ``src/<pkg>/migrations/``
#: path; the brief-writer and slicer prompts below name it for the
#: briefs they author.
MIGRATION_DDL_BRIEF = (
    "If your diff adds a file under `src/<pkg>/migrations/`, its DDL "
    "follows the squawk rules the land's gate runs on every new migration "
    "(`uvx -q --from 'squawk-cli>=2' squawk`; `.squawk.toml` at the repo "
    "root holds the rule set): `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE "
    "... ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`; "
    "`bigint`/`integer`, never `smallint`; seed `INSERT ... ON CONFLICT "
    "DO NOTHING`. `src/precis/migrations/0276_learning_events.sql` is the "
    "house exemplar — copy its shape. Before hand-off run "
    "`uvx -q --from 'squawk-cli>=2' squawk <new file>` and `scripts/test "
    "tests/test_schema_design.py tests/test_docs_docstring_lint.py` (a "
    "new jsonb column joins `JSONB_COLUMNS` with its rationale; a package "
    "`__init__.py` docstring stays under the 1500-word ceiling)."
)


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


def gripe_report_texts(store: Store, gid: int) -> list[str]:
    """The gripe's chunk texts in order, minus its hidden comments
    (``meta.hidden_comments``): a hidden comment is stale by the hider's
    ruling, so neither a build's embedded report nor the mint-time route
    reads it (gr653037 — gr573439 stayed held after its comment was hidden)."""
    from precis.handlers import _comment_timeline as tl

    ref = store.get_ref(kind="gripe", id=gid)
    hidden = tl.hidden_comments(ref) if ref is not None else set()
    return [
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(gid)
        if c.text and not (c.chunk_kind == "gripe_comment" and c.ord in hidden)
    ]


def _gripe_report(store: Store, gid: int, handle: str, title: str) -> str:
    """The gripe's visible chunks (body and comments, in order) verbatim at
    mint time, headed by its handle and title: what a lane without the
    precis MCP gets instead of ``get(kind='gripe')``."""
    parts = gripe_report_texts(store, gid)
    text = "\n\n".join(parts).strip()
    if len(text) > _DIRECT_REPORT_CAP:
        text = (
            text[:_DIRECT_REPORT_CAP]
            + "\n\n[report cut here at mint; the rest is on the gripe]"
        )
    return f"## {handle}: {title}\n\n{text}" if text else f"## {handle}: {title}"


#: The sentence ending :func:`_direct_build_body`'s instruction head —
#: ``precis.plans.route`` strips through it when it classifies a build
#: brief, so the head's own ``scripts/ship --quick`` mention never reads
#: as the fix's deny-path target (gr645163).
DIRECT_HEAD_END = (
    "a reviewer gates the land from another lane — the land mints on its LAND verdict."
)


def _direct_build_body(handle: str, report: str) -> str:
    return (
        f"Fix {handle} directly: no triage and no brief ran, and this lane may "
        "have no precis MCP, so the report is below verbatim as of mint. Find "
        "the code it names and implement the fix on this branch, following "
        "the adapter's finishing contract. "
        "Touch nothing under the lane's deny paths. The land (land_slice, "
        "`scripts/ship --quick`) squash-merges onto main and writes "
        f"'resolved: <sha>' + verify on {handle}; " + DIRECT_HEAD_END + "\n\n" + report
    )


def mint_gripe_fix_direct(
    store: Store,
    parent_id: int | None = None,
    *,
    gripe_id: Any,
    lane: str | None = None,
    claim_band: str | None = None,
    brief: str | None = None,
    chain_title: str | None = None,
    root_meta: dict[str, Any] | None = None,
) -> PlanMint:
    """:func:`mint_gripe_fix` with ``direct=True``: the ``gripe-fix-direct``
    template."""
    return mint_gripe_fix(
        store,
        parent_id,
        gripe_id=gripe_id,
        direct=True,
        lane=lane,
        claim_band=claim_band,
        brief=brief,
        chain_title=chain_title,
        root_meta=root_meta,
    )


def mint_gripe_fix(
    store: Store,
    parent_id: int | None = None,
    *,
    gripe_id: Any,
    direct: bool = False,
    lane: str | None = None,
    claim_band: str | None = None,
    brief: str | None = None,
    chain_title: str | None = None,
    root_meta: dict[str, Any] | None = None,
) -> PlanMint:
    """triage -> brief -> build -> gate sha-in-prod -> validate, under a phase
    root that is ``related-to`` the gripe (the gripe keeps the ticket state).

    A reviewer generator is blocked-by the build only and GATES the land
    (gr570506, Reto 2026-11-12: gripe fixes are reviewed BEFORE landing —
    the build's clean exit pushes its branch and holds the land_slice
    mint on the verdict; the sha-in-prod gate and the validate wait on
    the land's own ``landed:`` stamp as before).

    ``direct`` (the ``gripe-fix-direct`` template, td564019, Reto 2026-11-04:
    "the codex chain should run indep of claude") drops triage and brief:
    the build is the first node, its body carries the gripe's text verbatim
    (the lane may have no MCP), and no Claude step stands between the mint
    and a vendor-lane build. Review and validate still follow the build —
    the review before the land, the validate after it.
    ``lane`` pins the build's lane (``meta.op.params.lane``); unset, the
    class pick applies (``code`` -> the free vendor lanes first).

    ``claim_band`` (``release``) stamps the chain root so its build and
    land jobs claim in the release band (``plan_dispatch._mint_job``);
    ``brief`` replaces the direct build's default instruction (the
    gripe's report still follows it verbatim), ``chain_title`` the root and
    build titles, and ``root_meta`` is merged into the root's meta — the
    waiver gripe's chain and its revert chain (``plans/red_waiver``)
    use these."""
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
    root_title = chain_title or f"Fix {label}"
    build_title = chain_title or f"Build the fix for {handle}"
    build_op: Any = "run_slice"
    if lane:
        build_op = {"type": "run_slice", "params": {"lane": str(lane)}}

    report = _gripe_report(store, gid, handle, title)

    def build(m: _Minter) -> None:
        if direct:
            _build_direct(m)
        else:
            _build_staged(m)
        # gr604593: the chain root inherits the gripe's live dispatch
        # score (severity × dups × capped age × a human-prio multiplier
        # — the score, not a pseudo-prio).
        row = m.conn.execute(
            f"SELECT {_dscore.score_sql()} FROM refs r WHERE r.ref_id = %s",
            (gid,),
        ).fetchone()
        if row and row[0] is not None:
            m.store.stamp_ref_meta(
                m.ids["root"],
                {_dscore.SCORE_META_KEY: float(row[0])},
                conn=m.conn,
            )
        extra = dict(root_meta or {})
        if claim_band:
            extra[_dscore.CLAIM_BAND_META_KEY] = claim_band
        if extra:
            m.store.stamp_ref_meta(m.ids["root"], extra, conn=m.conn)

    def _build_direct(m: _Minter) -> None:
        m.node("root", root_title, _phase())
        m.node(
            "build",
            build_title,
            _task(build_op),
            body=(
                f"{brief}\n\n{report}" if brief else _direct_build_body(handle, report)
            ),
        )
        _tail(m)

    def _tail(m: _Minter) -> None:
        m.node(
            "review",
            f"Review the {handle} fix before land",
            _reviewer(m.ids["build"], preland=True),
            body=_reviewer_brief(m.ids["build"], preland=True),
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
                "node. Write acceptance tests before the build, make their "
                "files available via the build node's "
                "`meta.op.params.acceptance_tests` mapping of test paths to "
                "source text (the worker installs them after worktree cleanup), "
                "and name the test paths "
                "in that brief so the harness can run them after Devin exits. "
                "Nothing committed? "
                "End with `no-op: brief posted on td<build>` as the first "
                "line of your final message (gr561132).\n\nWhen the fix adds "
                "a migration the brief chunk also carries this paragraph "
                "verbatim (gr570072):\n\n" + MIGRATION_DDL_BRIEF
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


# -- spec-build --------------------------------------------------------------


#: A spec section address: ``<path>.md`` (a whole-file spec) or
#: ``<path>.md~<Heading>`` (one section — the factory's second input,
#: gr570506). ``docs/backlog/<slug>.md`` is the usual home.
_MD_SECTION_RE = re.compile(r"^\S+\.md(?:~.+)?$")


def mint_spec_build(
    store: Store,
    parent_id: int | None = None,
    *,
    section: Any,
    lane: str | None = None,
) -> PlanMint:
    """build -> pre-land review, for one spec section (gr570506, Reto
    2026-11-12): a spec section is a factory input — the build implements
    it and, in the SAME commit, retires the section to a redirect naming
    the code that now implements it (acceptance tests carry the what —
    the living spec — docstrings and comments the why, so spec and
    implementation cannot drift). ``section`` is a repo-relative md
    address: ``'docs/backlog/<slug>.md~<Heading>'`` retires one section,
    ``'<path>.md'`` retires the whole file to a redirect stub (the file
    stays, its body one line naming the implementation).
    The reviewer gates the squash onto main (:data:`PRELAND_REVIEW_KEY`);
    ``lane`` pins the build (``meta.op.params.lane``), unset the class
    pick applies. The submitter waits on the outcome with
    ``get(kind='todo', id=<build>, args={'wait': <seconds>})`` — the
    factory is pull-only, the wait is the notification."""
    section = str(section).strip()
    if not section or _MD_SECTION_RE.fullmatch(section) is None:
        raise BadInput(
            f"section={section!r} is not an md spec address",
            next="args={'template': 'spec-build', 'section': "
            "'docs/backlog/<slug>.md~<Heading>'}",
        )
    build_op: Any = "run_slice"
    if lane:
        build_op = {"type": "run_slice", "params": {"lane": str(lane)}}

    def build(m: _Minter) -> None:
        m.node("root", f"Build {section}", _phase())
        b = m.node(
            "build",
            f"Build {section}",
            _task(build_op),
            body=(
                f"The spec section `{section}` is this build's brief — read "
                "it in your worktree and implement what it specifies. In the "
                "SAME commit retire the spec: a `~<Heading>` section is "
                "replaced by one line redirecting to the code that now "
                "implements it (`<module>::<symbol>` and its acceptance "
                "test); a whole-file spec is rewritten to that same one-line "
                "redirect, never deleted. Acceptance tests carry "
                "the what — the living spec — and docstrings and comments "
                "the why; update them and one hop out (callers and callees) "
                "so it all still reads right (Reto 2026-11-12). The land "
                "(land_slice, `scripts/ship --quick`) squash-merges onto "
                "main; a reviewer gates it from another lane — the land "
                "mints on its LAND verdict."
            ),
        )
        m.node(
            "review",
            f"Review the {section} build before land",
            _reviewer(b, preland=True),
            body=_reviewer_brief(b, preland=True),
            blocked_by=("build",),
        )

    return _run(store, parent_id, build)


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
                "order lives in the links, never in a title or body line. "
                "A slice that adds a migration gets this paragraph verbatim "
                "in its body (gr570072):\n\n" + MIGRATION_DDL_BRIEF
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


#: The tag a release-pass root carries for the sha it cuts —
#: ``release-main:<sha12>`` (the ``pr-head:`` shape), the newest
#: fully-reviewed head ``release_poll`` computed (gr592271 — not
#: always the tip). ``release_poll`` mints no second pass for a sha
#: that already has a root in any state: a new pass needs the cleared
#: head to move (td573430 fix round 1).
RELEASE_MAIN_TAG_PREFIX = "release-main:"


def release_main_tag(head_sha: Any) -> str | None:
    """``release-main:<sha12>`` for a main head sha, else ``None``."""
    sha = str(head_sha or "").strip().lower()
    return f"{RELEASE_MAIN_TAG_PREFIX}{sha[:12]}" if sha else None


def mint_release_pass(
    store: Store, parent_id: int | None = None, *, main_sha: str | None = None
) -> PlanMint:
    """cut -> poll -> gate tests-green -> approval ask -> deploy -> confirm;
    a loop mints digest + one fixer per failure cluster (landing on
    ``release/r<N>``) + poll while red (td543733, td543855, td573432,
    release-train T1).

    ``cut`` (``release_cut``) runs ``scripts/release --cut-only`` and stamps
    r<N>/sha on the root (``meta.release``) and the real range on the gate
    and the loop's ``until``. ``poll`` (``ci_verdict_poll``) records the
    GitHub verdict, parking itself while CI runs. The gate and the loop are
    siblings blocked-by the poll, so the loop sees a recorded verdict: green
    closes it empty, red mints a fixer. ``approval`` is an ``answer`` gate
    that is also the ask Reto answers (``deploy r<N>`` | ``hold``) —
    minted with the ``ask-user`` tag only while ``release.auto_deploy``
    is disarmed; armed, the tick's standing approval answers it and a
    definitive refusal re-tags it
    (:func:`precis.plans.release.auto_answer_approval`, gr595272).
    ``deploy`` (``release_deploy``) refuses unless that answer names
    ``deploy r<N>`` (:func:`precis.plans.release.approval`). ``confirm``
    (``release_confirm``) then attests the deployed runtime — every live
    worker's heartbeat ``code_sha``, and the web's boot sha when
    ``release.health_url`` is set — before ``scripts/round`` confirms and
    closes the release; a failed probe hits the retry-cap ask
    (``retries: 0``), never a rollback. The root is
    tagged ``release-pass`` — the open-pass check
    ``release_poll`` reads before minting the next one — and, when
    ``main_sha`` is given, ``release-main:<sha12>``: the one-pass-per-sha
    claim the poll makes under its advisory lock."""
    gate = {"kind": "tests-green", "args": {"range": _release.PENDING_RANGE}}
    ask: dict[str, Any] = {
        "kind": "approval",
        "options": _release.approval_options("r<N>"),
        "blocks": "nothing",
        "context": "release/r<N> is cut by this plan; green CI gates this ask",
    }
    # gr595272: armed, the standing approval answers the ask — it stays
    # off the human queue until a definitive refusal re-tags it.
    human_ask = not _release.auto_deploy_armed(store)

    def build(m: _Minter) -> None:
        root = m.node("root", "Release pass", _phase())
        _tag(m, root, "release-pass")
        # The release-fixer class outranks routine builds (gr604593).
        m.store.stamp_ref_meta(
            root, {_dscore.SCORE_META_KEY: _dscore.RELEASE_SCORE}, conn=m.conn
        )
        stamped = release_main_tag(main_sha)
        if stamped is not None:
            _tag(m, root, stamped)
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
            "Fix on the release branch until green",
            {"loop": {"until": gate, "body": "release-fix"}},
            blocked_by=("poll",),
        )
        approval = m.node(
            "approval",
            _release.approval_title("r<N>"),
            {"gate": {"kind": "answer"}, "ask": ask},
            body=_release.approval_body("r<N>"),
            blocked_by=("gate",),
        )
        if human_ask:
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
        m.node(
            "confirm",
            "Confirm the release runtime",
            {**_task("release_confirm"), "retries": 0},
            body=(
                "release_confirm checks that every live worker's heartbeat "
                "code_sha (and the web's boot sha when release.health_url is "
                "set) equals the release sha, then runs scripts/round deploy "
                "--confirm-runtime then scripts/round close; a mismatch "
                "fails it and the tick's retry-cap ask goes to a human; no "
                "rollback."
            ),
            blocked_by=("deploy",),
        )
        # release-train T3: once the deploy ran, release/r<N> merges back
        # into main (a release_confirm mode, like the restamp is a poll
        # mode); the pass root stays open until it and the confirm close,
        # so release_poll mints the next pass only after both.
        merge_op: dict[str, Any] = {
            "type": "release_confirm",
            "params": {"merge_back": True},
        }
        mid = m.node(
            "merge_back",
            "Merge the release back into main",
            {"op": merge_op},
            body=(
                "release_confirm (merge_back mode) merges origin/release/r<N> "
                "into main with a real merge commit 'Merge release/r<N> fix "
                "forward' and pushes main; no commit past the cut is a no-op; "
                "a real conflict pushes nothing and mints one resolver todo "
                "plus an ours-merge node behind it; the next cut waits for "
                "both."
            ),
            blocked_by=("deploy",),
        )
        merge_op["params"]["todo_id"] = mid
        m.store.stamp_ref_meta(mid, {"op": merge_op}, conn=m.conn)
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


def _analysis_brief(store: Any, f: pr_class.PrFacts, why: str) -> str:
    from precis.plans.lanes import deny_paths_for

    n = f.number
    deny = "\n".join(f"- `{p}`" for p in deny_paths_for(store))
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
        m.store.stamp_ref_meta(
            root,
            {_dscore.SCORE_META_KEY: pr_class.score_of(cls, f)},
            conn=m.conn,
        )
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
                body=_analysis_brief(m.store, f, why),
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
            "(+ a pre-land review gating the land), for one gripe",
            mint_gripe_fix,
            optional=("lane",),
        ),
        Template(
            "gripe-fix-direct",
            ("gripe_id",),
            "build (the gripe's text in the body; no triage, no brief, no "
            "Claude step before a vendor lane) -> sha-in-prod gate -> validate "
            "(+ a pre-land review gating the land); 'lane' pins the build",
            mint_gripe_fix_direct,
            optional=("lane",),
        ),
        Template(
            "spec-build",
            ("section",),
            "build a spec section -> pre-land review; the same commit "
            "retires the section to a redirect (reflexive spec, gr570506)",
            mint_spec_build,
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
            "deploy; one fixer per failure cluster on release/r<N> while red",
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
        for tid in m.ids.values():
            finish_mint(store, tid, conn=conn)
    _post_comments(store, m.comments)
    return PlanMint(root_id=loop_todo_id, ids=dict(m.ids))


def _iteration_suffix(iteration: Any) -> str:
    return f" (iteration {iteration})" if iteration else ""


def _release_fix_meta(target: str | None) -> dict[str, Any]:
    """A release-fix node's meta: a ``run_slice`` task carrying the
    release branch it branches from and lands on (release-train T1)."""
    meta = _task()
    if target:
        meta[_release.LAND_TARGET_KEY] = target
    return meta


def _release_fix_body(target: str | None, cluster: str | None = None) -> str:
    """The fix node's brief. With a ``target`` it names ``release/r<N>``
    as the base and the land target (the ``land target:`` line is what
    :func:`precis.plans.release.land_target_of` reads); ``cluster`` names
    the failures this node owns when the digest split the red."""
    scope = (
        f"the failures of the cluster {cluster} it names"
        if cluster
        else "the failures it names"
    )
    head = (
        "Read the brief: comment on this todo -- the digest of the red "
        f"run's failed jobs -- and fix {scope}. "
    )
    if not target:
        return head + (
            "Land with scripts/ship --quick. Fix forward: never skip, xfail "
            "or loosen a test."
        )
    return head + (
        f"Base: origin/{target} -- the slice worktree is cut from the "
        f"release branch, not main. The lander lands the fix onto {target} "
        "(scripts/ship --onto), never main; main gets it when the release "
        "merges back after its deploy. Fix forward: never skip, xfail or "
        f"loosen a test.\nland target: {target}"
    )


def mint_release_fix(
    store: Store, loop_todo_id: int, *, iteration: int | None = None, **_ctx: Any
) -> PlanMint:
    """digest (``ci_red_digest``) -> fix(es) -> poll (``ci_verdict_poll``),
    td543855; the mechanical digest op replaced the LLM triage in
    td573431; the release train (``docs/backlog/release-train.md`` T1)
    dropped the re-cut and moved the fixes onto the release branch.

    The digest runs ``scripts/ci-failed`` on the plan's newest red
    check.yml run, clusters the failing ids (by test file, then by the
    failing ``src`` module) and posts one cluster's masked digest as a
    ``brief:`` comment per fix node -- the minted ``fix`` takes the first
    cluster, :func:`mint_release_fix_clusters` mints one sibling per
    further cluster, so the fixers run in parallel. The run_slice session
    reads the brief through ``run_slice._brief_parts``. Once the plan has
    a cut (``meta.release``), every fix node carries
    ``meta.land_target = 'release/r<N>'`` and its body names that branch
    as base and land target: ``run_slice`` cuts the worktree from it and
    ``land_slice`` lands onto it, never main. An INFRA red instead -- a
    completed run with no failing test in the logs (jobless,
    skipped-only, cancelled; ``ci_red_digest._infra_done``, gr611682) --
    never closes the fix node ``no-op``: the digest fires ``gh run
    rerun`` once and parks on it. A TEST red of 1-2 low-risk ids may
    instead be WAIVED by the digest (``precis.plans.red_waiver``, setting
    ``release.red_waiver``): the verdict is recorded green-waived, a
    ``broken-build`` gripe is filed, and the fix and poll nodes are
    closed ``waived: gr<N>`` -- no fix session.

    No re-cut. Blocked-by every fix node, the ``restamp`` node
    (``ci_verdict_poll`` in restamp mode) reads ``origin/release/r<N>``'s
    head and moves ``meta.release`` onto it
    (:func:`precis.plans.release.record_head`: sha, range end, the open
    ``tests-green`` gate and this loop's ``until``); the ``poll`` after it
    records the verdict of the check.yml run the fixes' push started. So
    the loop, which waits for every child to close, re-evaluates ``until``
    on the fixed head: green closes it and frees the approval ask, red
    mints the next iteration, whose digest reads the head's red run --
    never the cut's. T2's targeted ``release_rerun`` goes between the
    restamp and the poll."""
    _loop, root = _loop_context(store, loop_todo_id)
    suffix = _iteration_suffix(iteration)
    rel = _release.release_of(store, loop_todo_id)
    target = _release.release_branch(str(rel["rN"])) if rel else None

    def build(m: _Minter) -> None:
        m.node(
            "digest",
            f"Digest the red run{suffix}",
            _task("ci_red_digest"),
            body=(
                "ci_red_digest runs scripts/ci-failed on the plan's newest "
                "red check.yml run, clusters the failures and posts one "
                "cluster's masked digest as a brief: comment per blocked "
                "run_slice fix node (minting one more fix node per extra "
                "cluster)."
            ),
        )
        m.node(
            "fix",
            f"Fix the failures the digest found{suffix}",
            _release_fix_meta(target),
            body=_release_fix_body(target),
            blocked_by=("digest",),
        )
        restamp_op: dict[str, Any] = {
            "type": "ci_verdict_poll",
            "params": {"restamp": True},
        }
        rid = m.node(
            "restamp",
            f"Restamp the release onto its head{suffix}",
            {"op": restamp_op, "retries": _POLL_RETRIES},
            body=(
                "ci_verdict_poll (restamp mode) reads origin/release/r<N>'s head "
                "after the fixes landed and moves meta.release onto it — sha, "
                "range end, the tests-green gate and this loop's until — with "
                "no cut, so the poll and a later digest key on the fixed head."
            ),
            blocked_by=("fix",),
        )
        # The op names its own node: a dict op gets no todo_id from the tick.
        restamp_op["params"]["todo_id"] = rid
        m.store.stamp_ref_meta(rid, {"op": restamp_op}, conn=m.conn)
        m.node(
            "poll",
            f"Record the CI verdict on the release head{suffix}",
            {**_task("ci_verdict_poll"), "retries": _POLL_RETRIES},
            body=(
                "ci_verdict_poll reads check.yml's run on the release head's sha "
                "(the run the fixes' push started) from the GitHub API and "
                "records it in release.ci_verdicts."
            ),
            blocked_by=("restamp",),
        )

    return _run_body(store, loop_todo_id, root, build)


_ITERATION_RE = re.compile(r" \(iteration \d+\)$")


def mint_release_fix_clusters(
    store: Store, digest_id: int, fix_id: int, clusters: list[str]
) -> list[int]:
    """One more release-fix node per entry of ``clusters``, siblings of
    ``fix_id`` (same loop, plan tag, meta and land target), each
    ``blocked-by`` the digest, and every node that waits on ``fix_id``
    (the iteration's poll) made to wait on each new node too. One
    transaction; returns the new ids in ``clusters`` order. The digest
    (``ci_red_digest``) calls it when the red splits into more clusters
    than the body minted fix nodes for."""
    if not clusters:
        return []
    fix = store.get_ref(kind="todo", id=fix_id)
    if fix is None or fix.parent_id is None:
        raise NotFound(
            f"release-fix node td{fix_id} not found or has no loop parent",
            next="mint the iteration with the release-fix body first",
        )
    loop_id = int(fix.parent_id)
    target = _release.land_target_of(fix.meta)
    m_suffix = _ITERATION_RE.search(str(fix.title or ""))
    suffix = m_suffix.group(0) if m_suffix else ""
    with store.pool.connection() as conn:
        waiters = [
            int(r[0])
            for r in conn.execute(
                "SELECT src_ref_id FROM links WHERE relation = 'blocked-by' "
                "AND dst_ref_id = %s ORDER BY src_ref_id",
                (fix_id,),
            ).fetchall()
        ]
    with store.tx() as conn:
        m = _Minter(store, conn, loop_id)
        m.root_id = loop_id
        m.plan_root = _release.plan_root(store, fix_id)
        m.ids["digest"] = digest_id
        new: list[int] = []
        for i, cluster in enumerate(clusters, start=2):
            tid = m.node(
                f"fix{i}",
                f"Fix the failures in {cluster}{suffix}",
                _release_fix_meta(target),
                body=_release_fix_body(target, cluster),
                blocked_by=("digest",),
            )
            for waiter in waiters:
                store.add_link(
                    src_ref_id=waiter,
                    dst_ref_id=tid,
                    relation="blocked-by",
                    conn=conn,
                )
            new.append(tid)
        for tid in new:
            finish_mint(store, tid, conn=conn)
    return new


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
