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
#: ``(relpath, lineno) -> why it is safe``.
_ALLOWED_UNRESOLVED: dict[tuple[str, int], str] = {
    ("precis/diagram/handler.py", 296): "lang.kind — a diagram language's own ref kind",
    ("precis/handlers/_finding_acquire.py", 249): "kind param — finding mint",
    ("precis/handlers/_numeric_ref.py", 2524): (
        "self.kind on the generic numeric base — JobHandler stamps "
        "origin in its own put(), not here"
    ),
    ("precis/store/_cache_ops.py", 445): "kind param — cached-copy writes",
    ("precis/store/_draft_ops.py", 2060): "kind param — draft ops",
    ("precis/store/_value_entity_ops.py", 33): "kind param — value-entity kinds",
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
                elif resolved is None and (rel, call.lineno) not in _ALLOWED_UNRESOLVED:
                    bad.append(f"{rel}:{call.lineno} (unresolvable kind)")
        for lineno, text in _sql_strings(tree):
            if _INSERT_RE.search(text) and _JOB_LIT_RE.search(text):
                bad.append(f"{rel}:{lineno} (raw INSERT INTO refs with 'job')")
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
    """An unmarked put — an MCP session, a chat agent — is 'agent'
    (ruled 2026-10-23: a chat agent's put is not human)."""
    assert _meta(store, _put_job(hub))["origin"] == "agent"


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
