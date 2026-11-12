"""Contract: every minted ``kind='job'`` row carries ``meta.origin``.

Slice 1 of ``docs/backlog/job-scheduling-fairness.md`` (D1): the claim
order ranks jobs by origin class, so a job minted without a class
silently reads as the wrong one once class ranking lands. Two doors
stamp it — ``JobHandler.put`` (``human`` / ``agent`` / ``background``
from the write's producer) and
``precis.workers._mint.mint_background_job`` (``recurring`` /
``background``, derived). A direct ``insert_ref(kind='job')`` anywhere
else in ``src/`` fails the AST scan below; a new minter belongs in
``mint_background_job``.

gr595539 adds the rest of the mint-time matrix through the same two
doors (``_mint.job_mint_marks``): ``meta.share_key`` — the fair-share
axis the claim order will read — ``meta.task_kind``/``meta.consequence``
for a ``run_slice`` chain job, ``meta.size`` off the type's declared
size parameter, and ``meta.submitter`` when the mint runs inside a
bound MCP session. ``meta.lane`` is NOT minted: on a ``run_slice`` row
it is the execution lane ``submit`` records after its pick (td652988
review).
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.handlers.todo import TodoHandler
from precis.store import Store, as_background_actor
from precis.store.revision_context import revision_context
from precis.workers._mint import JOB_ORIGINS, mint_background_job

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Files allowed to call ``insert_ref`` for ``kind='job'`` directly:
#: the helper itself, and the put handler, which stamps origin inline.
_ALLOWED = {
    "precis/workers/_mint.py",
    "precis/handlers/job.py",
}

#: ``insert_ref`` sites whose ``kind`` the scanner cannot prove is not
#: ``'job'`` (a parameter passthrough, or an attribute of a generic
#: base/other object), individually verified to be non-job paths.
#: ``(relpath, enclosing qualname) -> (why it is safe, how many calls)`` —
#: anchored on the enclosing function, never a line number: code above
#: the call moving never shifts a qualname (gr515708). A second
#: unresolvable call in the same function trips the count, and a pin
#: naming a function that no longer has one is flagged stale.
_ALLOWED_UNRESOLVED: dict[tuple[str, str], tuple[str, int]] = {
    ("precis/diagram/handler.py", "DiagramHandler.put"): (
        "lang.kind — a diagram language's own ref kind",
        1,
    ),
    ("precis/cli/conventions.py", "_converge.ensure"): (
        "kind param — memory/finding manifest items only "
        "(_BODY_KIND[kind] has no 'job' entry)",
        1,
    ),
    ("precis/handlers/_finding_acquire.py", "put_acquiring"): (
        "kind param — finding mint",
        1,
    ),
    ("precis/handlers/_numeric_ref.py", "NumericRefHandler._create"): (
        "self.kind on the generic numeric base — JobHandler stamps "
        "origin in its own put(), not here",
        1,
    ),
    ("precis/store/_cache_ops.py", "CacheMixin.put_cache_entry"): (
        "kind param — cached-copy writes",
        1,
    ),
    ("precis/store/_draft_ops.py", "DraftStore.create_draft"): (
        "kind param — draft ops",
        1,
    ),
    ("precis/store/_value_entity_ops.py", "entity_upsert"): (
        "kind param — value-entity kinds",
        1,
    ),
}

_INSERT_RE = re.compile(r"INSERT\s+INTO\s+refs\b", re.IGNORECASE)
_JOB_LIT_RE = re.compile(r"['\"]job['\"]")


def _module_str_consts(tree: ast.Module) -> dict[str, str]:
    """Top-level ``NAME = 'str'`` assignments — for resolving ``kind=KIND``."""
    out: dict[str, str] = {}
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            out[target.id] = value.value
    return out


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    return {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}


def _class_str_const(cls: ast.ClassDef, name: str) -> str | None:
    """``name = 'str'`` assigned in a class body (``self.kind`` lookups)."""
    for node in cls.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign):
            target, value = node.target, node.value
        if (
            isinstance(target, ast.Name)
            and target.id == name
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        ):
            return value.value
    return None


def _resolve_kind(
    node: ast.expr | None,
    consts: dict[str, str],
    parents: dict[ast.AST, ast.AST],
    call: ast.Call,
) -> str | None:
    """Statically resolve a ``kind=`` value to a string, or ``None``.

    ``'job'``/``'todo'`` literals resolve; ``KIND`` resolves through a
    module-level constant; ``self.kind``/``cls.kind``/``self._KIND``
    resolve through the innermost enclosing class's attribute
    assignment. Anything else — a parameter passthrough, a call result,
    an attribute of another object — is unresolvable and flagged.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant):
        return node.value if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    ):
        cur = parents.get(call)
        while cur is not None:
            if isinstance(cur, ast.ClassDef):
                return _class_str_const(cur, node.attr)
            cur = parents.get(cur)
    return None


def _insert_ref_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "insert_ref"
    ]


def _enclosing_qualname(call: ast.Call, parents: dict[ast.AST, ast.AST]) -> str:
    """The dotted ``Class.func`` path enclosing the call — the anchor
    ``_ALLOWED_UNRESOLVED`` pins, stable to edits anywhere above the
    call site (gr515708)."""
    parts: list[str] = []
    cur: ast.AST | None = call
    while cur is not None:
        if isinstance(cur, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            parts.append(cur.name)
        cur = parents.get(cur)
    return ".".join(reversed(parts)) or "<module>"


def _sql_strings(tree: ast.Module) -> list[tuple[int, str]]:
    """``(lineno, text)`` of every string constant, joined-str parts
    concatenated — where a raw ``INSERT INTO refs`` could hide."""
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append((node.lineno, node.value))
        elif isinstance(node, ast.JoinedStr):
            out.append(
                (
                    node.lineno,
                    "".join(
                        p.value
                        for p in node.values
                        if isinstance(p, ast.Constant) and isinstance(p.value, str)
                    ),
                )
            )
    return out


def _bare_job_inserts() -> list[str]:
    """``file:line`` of every ``insert_ref`` call that is provably
    ``kind='job'`` outside the doors, that the scanner cannot prove is
    not a job and is not allowlisted, or a raw ``INSERT INTO refs``
    carrying a ``'job'`` literal."""
    bad: list[str] = []
    unresolved: dict[tuple[str, str], list[int]] = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if rel not in _ALLOWED:
            consts = _module_str_consts(tree)
            parents = _parents(tree)
            for call in _insert_ref_calls(tree):
                kind_arg = next(
                    (k.value for k in call.keywords if k.arg == "kind"),
                    call.args[0] if call.args else None,
                )
                resolved = _resolve_kind(kind_arg, consts, parents, call)
                if resolved == "job":
                    bad.append(f"{rel}:{call.lineno} (literal kind='job')")
                elif resolved is None:
                    key = (rel, _enclosing_qualname(call, parents))
                    unresolved.setdefault(key, []).append(call.lineno)
        for lineno, text in _sql_strings(tree):
            if _INSERT_RE.search(text) and _JOB_LIT_RE.search(text):
                bad.append(f"{rel}:{lineno} (raw INSERT INTO refs with 'job')")
    for (rel, qualname), linenos in sorted(unresolved.items()):
        pinned = _ALLOWED_UNRESOLVED.get((rel, qualname))
        if pinned is None:
            for ln in linenos:
                bad.append(f"{rel}:{ln} (unresolvable kind, in {qualname})")
        elif len(linenos) != pinned[1]:
            bad.append(
                f"{rel}:{qualname} has {len(linenos)} unresolvable "
                f"insert_ref calls, _ALLOWED_UNRESOLVED pins {pinned[1]}"
            )
    for (rel, qualname), (_why, count) in sorted(_ALLOWED_UNRESOLVED.items()):
        if len(unresolved.get((rel, qualname), [])) < count:
            bad.append(
                f"{rel}:{qualname} — _ALLOWED_UNRESOLVED pins {count} "
                f"unresolvable call(s) that are no longer there (stale pin)"
            )
    return bad


def test_every_job_insert_goes_through_an_origin_door() -> None:
    """A new minter writing ``insert_ref(kind='job')`` by hand — literally,
    positionally, through an unresolvable variable, or as raw SQL —
    fails here; it belongs in ``mint_background_job`` (or
    ``JobHandler.put``)."""
    bad = _bare_job_inserts()
    assert not bad, (
        "job-minting insert outside the origin-stamping doors "
        f"{sorted(_ALLOWED)}: {bad} — route the mint through "
        "precis.workers._mint.mint_background_job so meta.origin is stamped"
    )


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="job", id=ref_id)
    assert ref is not None
    return dict(ref.meta or {})


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' LIMIT 1",
            (ref_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _put_job(hub: Hub) -> int:
    todo_id = int(
        TodoHandler(hub=hub)
        .put(text="parent")
        .body.split("id=")[1]
        .split()[0]
        .rstrip(",.()")
    )
    resp = JobHandler(hub=hub).put(job_type="graph_garden", parent_id=todo_id)
    m = re.search(r"job id=(\d+)", resp.body)
    assert m is not None
    return int(m.group(1))


def test_mint_background_job_stamps_class_and_queued(store: Store) -> None:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="embed_batch",
            executor="job_inproc",
            title="t",
            params={"limit": 10},
            idem_key="k1",
            prio=8,
        )
    meta = _meta(store, int(ref.id))
    assert meta["origin"] == "background"
    assert meta["idem_key"] == "k1"
    assert meta["params"] == {"limit": 10}
    assert _status(store, int(ref.id)) == "queued"


def test_dispatched_from_todo_mints_recurring(store: Store) -> None:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="plan_tick",
            executor="claude_inproc",
            title="t",
            meta_extra={"dispatched_from_todo": 7},
        )
    assert _meta(store, int(ref.id))["origin"] == "recurring"


def test_explicit_origin_overrides_derivation(store: Store) -> None:
    """The retry door: a caller may carry a parent's class (graph_garden's
    bounded retry), never invent one outside the vocabulary."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="graph_garden",
            executor="job_inproc",
            title="t",
            origin="agent",
        )
        with pytest.raises(ValueError, match="origin must be one of"):
            mint_background_job(
                store, conn=conn, job_type="x", executor="y", title="t", origin="cron"
            )
    assert _meta(store, int(ref.id))["origin"] == "agent"


def test_origin_vocabulary_matches_spec() -> None:
    """D1's closed set, in claim-rank order (human first, background last)."""
    assert JOB_ORIGINS == ("human", "agent", "recurring", "background")


def test_put_stamps_agent_for_a_plain_mcp_put(hub: Hub, store: Store) -> None:
    """A dispatch-context put — an MCP session, a chat agent — is 'agent'
    (ruled 2026-10-23: a chat agent's put is not human)."""
    with revision_context(actor="agent"):
        job_id = _put_job(hub)
    assert _meta(store, job_id)["origin"] == "agent"


def test_worker_put_inherits_parent_job_origin(hub: Hub, store: Store) -> None:
    """A direct handler put (a worker minting with no dispatch context —
    ruled 2026-10-23, option a) takes its parent job's origin."""
    with store.tx() as conn:
        parent = mint_background_job(
            store,
            conn=conn,
            job_type="plan_tick",
            executor="coordinator",
            title="p",
            origin="agent",
        )
    resp = JobHandler(hub=hub).put(job_type="graph_garden", parent_id=int(parent.id))
    m = re.search(r"job id=(\d+)", resp.body)
    assert m is not None
    assert _meta(store, int(m.group(1)))["origin"] == "agent"


def test_worker_put_without_parent_job_is_background(hub: Hub, store: Store) -> None:
    """A direct put whose parent isn't a job (todo/quest — the quest
    compute/loop and pathway mints) falls back to 'background'."""
    assert _meta(store, _put_job(hub))["origin"] == "background"


def test_put_stamps_human_inside_human_write_context(hub: Hub, store: Store) -> None:
    """The web UI's ``human_write_context`` (deps.py) is the only 'human'
    door: a logged-in write with ``trust_actor_type='human'``."""
    with revision_context(
        actor="user:test", trust_origin="human", trust_actor_type="human"
    ):
        job_id = _put_job(hub)
    assert _meta(store, job_id)["origin"] == "human"


def test_put_stamps_background_inside_a_background_loop(hub: Hub, store: Store) -> None:
    """A put from inside a background loop (watch/dream) is 'background',
    not 'agent' — the loop is nobody's session."""
    with as_background_actor("watch"):
        job_id = _put_job(hub)
    assert _meta(store, job_id)["origin"] == "background"


def test_worker_put_inside_revision_context_still_inherits(
    hub: Hub, store: Store
) -> None:
    """A job parent wins over the write context: a worker put inside a
    ``revision_context`` still takes the parent job's class, not the
    context's — only job-parented fan-out can do this (job parents are
    coordinator-only)."""
    with store.tx() as conn:
        parent = mint_background_job(
            store,
            conn=conn,
            job_type="plan_tick",
            executor="coordinator",
            title="p",
            origin="recurring",
        )
    with revision_context(actor="agent"):
        resp = JobHandler(hub=hub).put(
            job_type="graph_garden", parent_id=int(parent.id)
        )
    m = re.search(r"job id=(\d+)", resp.body)
    assert m is not None
    assert _meta(store, int(m.group(1)))["origin"] == "recurring"


# ── gr595539: share_key / task_kind / consequence / size / submitter ──


def test_mint_stamps_share_key_and_size(store: Store) -> None:
    """Every minted row carries ``meta.share_key``; a type with a
    declared size parameter stamps ``meta.size = {name, value}`` off
    params. ``meta.lane`` stays off the row — it is the execution lane
    a ``run_slice`` stamps at submit, not a mint mark (td652988)."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="embed_batch",
            executor="job_inproc",
            title="t",
            params={"limit": 10},
        )
        bare = mint_background_job(
            store,
            conn=conn,
            job_type="plan_tick",
            executor="claude_inproc",
            title="t",
        )
    meta = _meta(store, int(ref.id))
    assert meta["share_key"] == "embed_batch"
    assert meta["size"] == {"name": "limit", "value": 10}
    assert "lane" not in meta
    bare_meta = _meta(store, int(bare.id))
    assert bare_meta["share_key"] == "plan_tick"
    assert "size" not in bare_meta


def test_mint_share_key_discriminates_on_params_pass(store: Store) -> None:
    """A ``params.pass`` sub-queue shares the claim lane — the stamped
    key is ``<job_type>:<pass>``, the claim's own ``_share_key``."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="derived_drain",
            executor="job_inproc",
            title="t",
            params={"pass": "small", "limit": 25},
        )
    meta = _meta(store, int(ref.id))
    assert meta["share_key"] == "derived_drain:small"
    assert meta["size"] == {"name": "limit", "value": 25}


def test_mint_size_folds_a_list_param(store: Store) -> None:
    """A list-valued size parameter stamps its length — ``ref_ids``'s
    size is how many refs, not which ones."""
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="cite_resolve",
            executor="job_inproc",
            title="t",
            params={"ref_ids": [11, 22, 33]},
        )
    assert _meta(store, int(ref.id))["size"] == {"name": "ref_ids", "value": 3}


def _mint_run_slice(
    store: Store, task_class: str | None, parent_id: int | None = None
) -> dict[str, Any]:
    """A run_slice job whose ``params.todo_id`` is a todo carrying
    ``meta.task_class`` (or none) — returns the job's stamped meta.
    ``parent_id`` defaults to the same todo; pass another ref to pin the
    differently-parented cases."""
    todo_meta = {"task_class": task_class} if task_class else {}
    todo = store.insert_ref(kind="todo", slug=None, title="slice", meta=todo_meta)
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="t",
            params={"todo_id": int(todo.id), "effort": "high"},
            parent_id=int(todo.id) if parent_id is None else parent_id,
        )
    return _meta(store, int(ref.id))


def test_run_slice_mint_stamps_recipe_marks(store: Store) -> None:
    """A chain job's share key is the recipe's lane_class for the
    executed todo's task_class; task_kind/consequence ride the same
    row. ``meta.lane`` stays off the row — submit stamps the execution
    lane there."""
    meta = _mint_run_slice(store, "review")
    assert meta["share_key"] == "review"
    assert meta["task_kind"] == "code.review"
    assert meta["consequence"] == "high"
    assert meta["size"] == {"name": "effort", "value": "high"}
    assert "lane" not in meta


def test_run_slice_mint_defaults_for_an_unreciped_class(store: Store) -> None:
    """A task_class with no recipe keeps itself as the share key and
    reads ``unknown.<class>``/``medium`` — the route_log unknown-source
    convention."""
    meta = _mint_run_slice(store, "exotic_lane")
    assert meta["share_key"] == "exotic_lane"
    assert meta["task_kind"] == "unknown.exotic_lane"
    assert meta["consequence"] == "medium"


def test_run_slice_mint_defaults_task_class_to_code(store: Store) -> None:
    """No ``meta.task_class`` on the executed todo → the run_slice
    default ``code``, same as the job's own submit path."""
    meta = _mint_run_slice(store, None)
    assert meta["share_key"] == "code"
    assert meta["task_kind"] == "code.implement"
    assert meta["consequence"] == "high"


def test_run_slice_mint_class_comes_from_params_todo_id(store: Store) -> None:
    """The class lives on the todo the job EXECUTES, not its parent
    (td652988 review): a run_slice job parented to a coordinator job —
    which carries no ``task_class`` — still stamps the recipe row of
    the todo ``params.todo_id`` names, and a job parented to a
    different todo stamps ITS ``params.todo_id``, not the parent's."""
    coordinator = store.insert_ref(kind="job", slug=None, title="coord", meta={})
    meta = _mint_run_slice(store, "review", parent_id=int(coordinator.id))
    assert meta["share_key"] == "review"
    assert meta["task_kind"] == "code.review"
    assert meta["consequence"] == "high"
    other_todo = store.insert_ref(
        kind="todo", slug=None, title="other", meta={"task_class": "docs"}
    )
    meta = _mint_run_slice(store, "review", parent_id=int(other_todo.id))
    assert meta["share_key"] == "review"
    assert meta["task_kind"] == "code.review"


def test_mint_stamps_no_submitter_outside_a_session(store: Store) -> None:
    """``meta.submitter`` stamps only where a session exists — a worker
    tick / CLI mint leaves it off the row entirely."""
    with store.tx() as conn:
        ref = mint_background_job(
            store, conn=conn, job_type="plan_tick", executor="x", title="t"
        )
    assert "submitter" not in _meta(store, int(ref.id))


def test_mint_stamps_submitter_inside_a_session(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mint inside a bound MCP session records ``{session_id, client}``
    — the same session the tool_call ledger joins on."""
    from types import SimpleNamespace

    from precis import serve_ledger

    session = SimpleNamespace(
        client_params=SimpleNamespace(
            clientInfo=SimpleNamespace(name="claude-code", version="1.0"),
            meta=SimpleNamespace(model="opus"),
        )
    )
    monkeypatch.setattr(serve_ledger, "current_session", lambda: session)
    with store.tx() as conn:
        ref = mint_background_job(
            store, conn=conn, job_type="plan_tick", executor="x", title="t"
        )
    submitter = _meta(store, int(ref.id))["submitter"]
    assert submitter["session_id"]
    assert submitter["client"] == "claude-code/1.0 (model:opus)"


def test_put_stamps_the_mint_marks(hub: Hub, store: Store) -> None:
    """The put door derives the same matrix — share_key (and size when
    the type declares one) land on the row through ``JobHandler.put``
    too."""
    meta = _meta(store, _put_job(hub))
    assert meta["share_key"] == "graph_garden"
    assert "size" not in meta
