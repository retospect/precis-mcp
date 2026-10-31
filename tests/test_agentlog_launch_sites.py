"""Ratchet: every agent launch site binds an agentlog (gr540852).

A *launch site* is an ``LlmRequest(...)`` / ``call_claude_agent(...)`` /
``call_claude_agent_async(...)`` call under ``src/`` that hands the agent a
precis MCP server (an ``mcp_config=`` keyword that is not the literal
``None``) — the runs whose ``tool_calls`` rows need ``agentlog_id`` and
``source``. ``source`` rides ``LlmRequest.source`` (the router adds
``PRECIS_CURRENT_SOURCE``); the agentlog id must be put in the request's
env by the runner — the evidence the ratchet demands is AST-level, in the
site's enclosing function: a ``Call`` to ``open_log`` / ``open_run`` /
``run_env``, or a reference to ``ENV_VAR`` / ``agentlog_id``. A comment,
docstring or bare ``env_overlay`` keyword that carries no run id does not
count, and a ``<module>``-level site sees only the module's own top-level
code — not a sibling def's binding.

A launch site in a file that does neither fails here unless it is listed in
:data:`PENDING`. ``PENDING`` only shrinks: an entry that now binds an
agentlog, or no longer launches an agent, fails too, so the list stays true.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

_LAUNCH_CALLS = frozenset(
    {"LlmRequest", "call_claude_agent", "call_claude_agent_async"}
)

#: Files that define the transport itself, not a runner.
_TRANSPORT_FILES = frozenset(
    {
        "src/precis/utils/llm/router.py",
        "src/precis/utils/claude_agent.py",
        "src/precis/utils/llm/requirement.py",
    }
)

#: Calls that mint a run record or build its env binding.
_MINT_CALLS = frozenset({"open_log", "open_run", "run_env"})

#: Identifier/attribute/keyword references that carry the minted id.
_BIND_NAMES = frozenset({"ENV_VAR", "agentlog_id"})

#: Agent launch sites that do not mint an agentlog yet. Shrink-only: fix one
#: (``agentlog.open_run`` + ``env_overlay=agentlog.run_env(log_id)``) and
#: delete its line.
PENDING: dict[str, str] = {
    # Slack bridge — separate daemon; asa_bot's mint pattern is a follow-up.
    "src/asa_slack/bot.py": "Slack bridge turn; mirror asa_bot.claude_invoke",
    # mermaid/figure helper — no Store in scope to mint against.
    "src/precis/diagram/agent.py": "diagram agent turn",
    # Interactive web follow-up — its deferred path drops env_overlay.
    "src/precis_web/ask.py": "web follow-up chat",
}

#: The runners gr540852 fixed plus the two that already bound one; each must
#: stay a compliant launch site (guards against the scan going blind).
REQUIRED: tuple[str, ...] = (
    "src/precis/workers/job_types/plan_tick.py",
    "src/precis/workers/dream_agent.py",
    "src/precis/workers/job_types/doctor_tick.py",
    "src/precis/workers/review.py",
    "src/asa_bot/claude_invoke.py",
)


def _call_name(node: ast.Call) -> str | None:
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def _launches_agent(node: ast.Call) -> bool:
    if _call_name(node) not in _LAUNCH_CALLS:
        return False
    for kw in node.keywords:
        if kw.arg == "mcp_config":
            return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)
    return False


class _Site:
    """One launch site plus the scope the evidence check runs on."""

    def __init__(self, rel: str, call: ast.Call, qualname: str, scope: ast.AST) -> None:
        self.rel = rel
        self.call = call
        self.qualname = qualname
        # The enclosing FunctionDef/AsyncFunctionDef, or the whole Module
        # for a <module>-level site.
        self.scope = scope

    @property
    def attributed(self) -> bool:
        """Whether the enclosing scope mints or threads an agentlog."""
        if isinstance(self.scope, ast.Module):
            return _attribution_evidence(_module_level_nodes(self.scope))
        return _attribution_evidence(ast.walk(self.scope))


def _attribution_evidence(nodes: Iterable[ast.AST]) -> bool:
    """The ratchet's evidence grammar: a ``Call`` to ``open_log`` /
    ``open_run`` / ``run_env`` (mint or env binding), or an ``ENV_VAR`` /
    ``agentlog_id`` name, attribute or call keyword — the mechanisms that
    put ``PRECIS_CURRENT_AGENTLOG`` on the run.

    AST-only, so a comment, docstring or bare ``env_overlay`` keyword
    carrying no run id cannot satisfy it.
    """
    for node in nodes:
        if isinstance(node, ast.Call) and _call_name(node) in _MINT_CALLS:
            return True
        if isinstance(node, ast.Name) and node.id in _BIND_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in _BIND_NAMES:
            return True
        if isinstance(node, ast.keyword) and node.arg in _BIND_NAMES:
            return True
    return False


def _module_level_nodes(tree: ast.Module) -> Iterator[ast.AST]:
    """Nodes of the module's own top-level code, never descending into a
    ``def`` / ``class`` body — a ``<module>``-scoped site must not bind on
    a sibling function's internals (the old whole-file regex in AST form)."""
    stack: list[ast.AST] = list(tree.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _encloses(fn: ast.AST, node: ast.AST) -> bool:
    return getattr(fn, "lineno", -1) <= getattr(node, "lineno", -1) and getattr(
        fn, "end_lineno", -1
    ) >= getattr(node, "end_lineno", -1)


def _qualname(tree: ast.AST, target: ast.AST) -> str:
    """``outer.inner`` dotted name for a (possibly nested) function def."""
    names: list[str] = []

    def visit(node: ast.AST, path: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if child is target:
                names.extend(path + [getattr(child, "name", "?")])
                return
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                visit(child, path + [child.name])
            else:
                visit(child, path)

    visit(tree, [])
    return ".".join(names) or getattr(target, "name", "?")


def _sites_in_source(rel: str, text: str) -> list[_Site]:
    """Launch sites in one source string — the seam the fixture tests
    drive without touching real files."""
    tree = ast.parse(text, filename=rel)
    sites: list[_Site] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not _launches_agent(node):
            continue
        # The OUTERMOST function enclosing the call — a mint/threading in
        # any ancestor scope counts (helpers thread the id in via
        # parameters or closures). ast.walk is breadth-first, so the
        # first enclosing def is the outermost.
        outer: ast.AST | None = None
        for anc in ast.walk(tree):
            if isinstance(anc, ast.FunctionDef | ast.AsyncFunctionDef) and _encloses(
                anc, node
            ):
                outer = anc
                break
        if outer is None:
            sites.append(_Site(rel, node, "<module>", tree))
        else:
            sites.append(_Site(rel, node, _qualname(tree, outer), outer))
    return sites


def _launch_sites() -> list[_Site]:
    sites: list[_Site] = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        if rel in _TRANSPORT_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if not any(name + "(" in text for name in _LAUNCH_CALLS):
            continue
        sites.extend(_sites_in_source(rel, text))
    return sites


SITE_LIST = _launch_sites()
SITES: dict[str, list[int]] = {}
for _s in SITE_LIST:
    SITES.setdefault(_s.rel, []).append(_s.call.lineno)


def _unbound_sites(rel: str) -> list[_Site]:
    return [s for s in SITE_LIST if s.rel == rel and not s.attributed]


def _binds(rel: str) -> bool:
    """Every launch site in ``rel`` carries AST-level attribution evidence."""
    return not _unbound_sites(rel)


def test_scan_finds_the_known_runners() -> None:
    missing = [rel for rel in REQUIRED if rel not in SITES]
    assert not missing, f"launch-site scan no longer sees {missing}"


def test_every_launch_site_binds_an_agentlog() -> None:
    offenders = [
        f"{s.rel}::{s.qualname}:{s.call.lineno}"
        for s in SITE_LIST
        if s.rel not in PENDING and not s.attributed
    ]
    assert not offenders, (
        "agent launch sites with no agentlog in scope (their tool_calls "
        "rows get no agentlog_id): "
        + ", ".join(offenders)
        + ". Mint one with agentlog.open_run(...) and pass "
        "env_overlay=agentlog.run_env(log_id) (gr540852)."
    )


def test_pending_only_shrinks() -> None:
    gone = [rel for rel in PENDING if rel not in SITES]
    fixed = [rel for rel in PENDING if rel in SITES and _binds(rel)]
    assert not gone, f"PENDING entries no longer launch an agent; delete: {gone}"
    assert not fixed, f"PENDING entries now bind an agentlog; delete: {fixed}"


# ── behaviour: the fixed runners put the id in the request env ──────────


class _Res:
    error = None
    text = ""
    raw_text = ""
    duration_s = 0.0
    cost_usd = None
    model = None
    paused = False
    timed_out = False
    terminal_reason = None


def _stub_agentlog(monkeypatch: pytest.MonkeyPatch, log_id: int) -> list[Any]:
    from precis import agentlog

    closed: list[Any] = []
    monkeypatch.setattr(agentlog, "open_run", lambda store, **kw: log_id)
    monkeypatch.setattr(
        agentlog, "close_run", lambda store, lid, **kw: closed.append((lid, kw))
    )
    return closed


def test_doctor_tick_passes_its_agentlog(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.agentlog import ENV_VAR
    from precis.utils.llm import router
    from precis.workers.job_types import doctor_tick

    closed = _stub_agentlog(monkeypatch, 4242)
    seen: list[Any] = []

    def fake_route(req: Any) -> _Res:
        seen.append(req)
        return _Res()

    monkeypatch.setattr(router, "route", fake_route)
    doctor_tick.run(store=object(), job_ref_id=7, params={})
    assert seen, "doctor_tick did not dispatch"
    assert seen[0].env_overlay == {ENV_VAR: "4242"}
    assert seen[0].source == "doctor_tick"
    assert closed and closed[0][0] == 4242


def test_review_passes_its_agentlog(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.agentlog import ENV_VAR
    from precis.workers import review

    closed = _stub_agentlog(monkeypatch, 99)
    seen: list[Any] = []

    def fake_route(req: Any) -> _Res:
        seen.append(req)
        raise RuntimeError("stop after dispatch")

    from precis.workers.structural import STRUCTURAL as reviewer

    monkeypatch.setattr(review, "_gate_enabled", lambda env: True)
    monkeypatch.setattr(review, "skip_if_high_load", lambda name: False)
    monkeypatch.setattr(review, "_recent_digest_exists", lambda *a: False)
    monkeypatch.setattr(review, "_recent_failure", lambda *a: False)
    monkeypatch.setattr(review, "_assemble_reviewer_blocks", lambda *a: [])
    monkeypatch.setattr(
        review.ClaudeAgentAdapter, "render", staticmethod(lambda blocks: ("", "p"))
    )
    monkeypatch.setattr(review, "route", fake_route)
    store: Any = object()
    with pytest.raises(RuntimeError, match="stop after dispatch"):
        review.run_review_pass(reviewer, store)
    assert seen[0].env_overlay == {ENV_VAR: "99"}
    assert seen[0].source == f"review:{reviewer.name}"
    assert not closed  # the stub raised before close; real route never raises


def test_asa_turn_overlay_carries_the_agentlog() -> None:
    from types import SimpleNamespace

    from asa_bot.claude_invoke import _turn_overlay
    from precis.agentlog import ENV_VAR

    cfg: Any = SimpleNamespace(env={"CLAUDE_CODE_OAUTH_TOKEN": "x"})
    overlay = _turn_overlay(cfg, "conv-1", None, 77)
    assert overlay[ENV_VAR] == "77"
    assert ENV_VAR not in _turn_overlay(cfg, "conv-1", None, None)


def test_asa_invoke_mints_one_log_per_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import asyncio

    from asa_bot import claude_invoke
    from asa_bot.config import LLMConfig
    from precis.agentlog import ENV_VAR
    from precis.utils.llm.router import LlmResult, Tier

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("asa_bot.secrets.reveal_secret", lambda name, **kw: None)
    monkeypatch.setattr(claude_invoke, "warm_runtime", lambda: None)
    opened: list[tuple[str, ...]] = []
    closed: list[int] = []

    def fake_open(model: str, slug: str, msg: str) -> int:
        opened.append((model, slug, msg))
        return 55

    monkeypatch.setattr(claude_invoke, "_open_turn_log", fake_open)
    monkeypatch.setattr(
        claude_invoke, "_close_turn_log", lambda lid, res, **kw: closed.append(lid)
    )
    seen: list[Any] = []

    def fake_route(req: Any) -> LlmResult:
        seen.append(req)
        return LlmResult(
            text="ok", cost_usd=0.0, turns_used=1, model="m", tier=Tier.BIG
        )

    monkeypatch.setattr(claude_invoke, "route", fake_route)
    asyncio.run(claude_invoke.invoke(LLMConfig(), "sys", "hi", conv_slug="c-1"))
    assert opened == [("local", "c-1", "hi")]
    assert seen[0].env_overlay[ENV_VAR] == "55"
    assert seen[0].source == "asa_bot"
    assert closed == [55]


def test_container_allowlist_carries_agentlog_and_source() -> None:
    """The containerised path forwards both keys and never the token."""
    from precis.agentlog import ENV_VAR, SOURCE_ENV_VAR
    from precis.workers.executors.agent_container import run_context_env

    env = run_context_env(
        {ENV_VAR: "5", SOURCE_ENV_VAR: "asa_bot", "CLAUDE_CODE_OAUTH_TOKEN": "s"}
    )
    assert env == {ENV_VAR: "5", SOURCE_ENV_VAR: "asa_bot"}


# ── the evidence grammar is AST-level (gr540852 review) ─────────────────


def test_comment_or_docstring_mention_is_not_attribution() -> None:
    """Negative fixture: a function that only *mentions* agentlog in a
    comment or docstring fails the ratchet — evidence must be an AST
    mint call or a bind name in the enclosing function."""
    src = (
        "def run():\n"
        '    """Mint the agentlog for this pass."""\n'
        "    # agentlog.open_run(store, source='x') would go here\n"
        "    return LlmRequest(tools_needed=True, mcp_config='m.json')\n"
    )
    (site,) = _sites_in_source("fixture.py", src)
    assert site.qualname == "run"
    assert not site.attributed


def test_mint_call_and_bind_name_count_as_attribution() -> None:
    """Positive fixtures, one per evidence form."""
    minted = (
        "def run():\n"
        "    log_id = agentlog.open_run(store, source='x', title='t')\n"
        "    return LlmRequest(tools_needed=True, mcp_config='m.json')\n"
    )
    (site,) = _sites_in_source("fixture.py", minted)
    assert site.attributed

    threaded = (
        "def run():\n"
        "    return LlmRequest(\n"
        "        tools_needed=True,\n"
        "        mcp_config='m.json',\n"
        "        env_overlay=agentlog.run_env(log_id),\n"
        "    )\n"
    )
    (site,) = _sites_in_source("fixture.py", threaded)
    assert site.attributed

    contextvar = (
        "def run():\n"
        "    ctx = TickContext(agentlog_id=agentlog_id)\n"
        "    with tick_context(ctx):\n"
        "        return LlmRequest(tools_needed=True, mcp_config='m.json')\n"
    )
    (site,) = _sites_in_source("fixture.py", contextvar)
    assert site.attributed


def test_bare_env_overlay_keyword_is_not_attribution() -> None:
    """An ``env_overlay`` carrying no run id (the asa_slack shape —
    ``PRECIS_KINDS_DISABLED`` only) is not evidence of a minted agentlog."""
    src = (
        "def run():\n"
        "    return LlmRequest(\n"
        "        tools_needed=True,\n"
        "        mcp_config='m.json',\n"
        "        env_overlay={'PRECIS_KINDS_DISABLED': 'x'},\n"
        "    )\n"
    )
    (site,) = _sites_in_source("fixture.py", src)
    assert not site.attributed


def test_module_level_site_cannot_borrow_a_sibling_def() -> None:
    """A ``<module>``-level site must not get the whole file as its
    scope: a sibling function's ``ENV_VAR`` is not the module's."""
    src = (
        "req = LlmRequest(tools_needed=True, mcp_config='m.json')\n"
        "\n"
        "def helper():\n"
        "    env = {ENV_VAR: '1'}\n"
    )
    (site,) = _sites_in_source("fixture.py", src)
    assert site.qualname == "<module>"
    assert not site.attributed


# ── asa_bot attribution store / turn-log behavior (gr540852 review) ──────


@pytest.fixture
def _run_store_state(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolate the module-level attribution pool state per test — a prior
    test's bound Store or connect-failure timestamp mustn't leak in."""
    import asa_bot.claude_invoke as ci

    monkeypatch.setattr(ci, "_run_store", None)
    monkeypatch.setattr(ci, "_run_store_failed_at", None)
    return ci


def test_attribution_store_backs_off_after_connect_failure(
    monkeypatch: pytest.MonkeyPatch, _run_store_state: Any
) -> None:
    """A failed connect is cached for ``_RUN_STORE_RETRY_S``: two turns
    inside the window pay ONE connect attempt, and that attempt runs on
    the short connect budget (gr540852 review: DB-down latency)."""
    ci = _run_store_state
    import precis.store.pool as pool_mod

    monkeypatch.setenv("PRECIS_DATABASE_URL", "postgresql://unused.invalid/x")
    seen: dict[str, Any] = {}
    attempts = 0

    def fake_create_pool(*args: Any, **kwargs: Any) -> Any:
        nonlocal attempts
        attempts += 1
        seen.update(kwargs)
        raise OSError("db is down")

    monkeypatch.setattr(pool_mod, "create_pool", fake_create_pool)

    assert ci._open_turn_log("m", "c1", "u") is None
    assert ci._open_turn_log("m", "c2", "u") is None
    assert attempts == 1
    assert seen["open_timeout"] == ci._RUN_STORE_CONNECT_S
    assert seen["kwargs"]["connect_timeout"] == int(ci._RUN_STORE_CONNECT_S)


def test_open_turn_log_stores_fingerprint_not_prompt(
    monkeypatch: pytest.MonkeyPatch, _run_store_state: Any
) -> None:
    """Privacy (gr540852 review): the asa agentlog meta carries the conv
    slug + a sha256/byte-length fingerprint of the user message — never
    ``meta.prompt``; Discord chat text is not persisted per turn."""
    ci = _run_store_state
    import hashlib

    from precis import agentlog

    monkeypatch.setattr(ci, "_run_store", object())
    captured: dict[str, Any] = {}

    def fake_open_run(store: Any, **kwargs: Any) -> int:
        captured.update(kwargs)
        return 42

    monkeypatch.setattr(agentlog, "open_run", fake_open_run)

    assert ci._open_turn_log("m", "conv-x", "hello?") == 42
    assert "prompt" not in captured
    extra = captured["meta_extra"]
    assert extra["conv_slug"] == "conv-x"
    assert extra["prompt_sha256"] == hashlib.sha256(b"hello?").hexdigest()[:16]
    assert extra["prompt_bytes"] == len(b"hello?")


def test_shutdown_attribution_store_closes_pool(
    monkeypatch: pytest.MonkeyPatch, _run_store_state: Any
) -> None:
    """bot.run's finally path: the lazily-bound pool is closed instead of
    left to the atexit finalizer — and the hook is idempotent."""
    ci = _run_store_state
    from types import SimpleNamespace

    closed: list[bool] = []
    monkeypatch.setattr(
        ci, "_run_store", SimpleNamespace(close=lambda: closed.append(True))
    )

    ci.shutdown_attribution_store()
    ci.shutdown_attribution_store()

    assert closed == [True]


def test_invoke_finalizes_failed_on_raise(
    monkeypatch: pytest.MonkeyPatch, _run_store_state: Any
) -> None:
    """invoke()'s try/finally stamps the minted agentlog ``failed`` when a
    lane raises through its never-raise contract (gr540852 review)."""
    ci = _run_store_state
    import asyncio

    from precis import agentlog

    monkeypatch.setattr(ci, "_run_store", object())
    monkeypatch.setattr(ci, "_open_turn_log", lambda *a: 7)
    stamped: dict[str, Any] = {}

    def fake_close_run(store: Any, lid: int, **kwargs: Any) -> None:
        stamped["log_id"] = lid
        stamped.update(kwargs)

    monkeypatch.setattr(agentlog, "close_run", fake_close_run)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("lane exploded")

    monkeypatch.setattr(ci, "_run_turn", boom)

    from asa_bot.config import LLMConfig

    with pytest.raises(RuntimeError):
        asyncio.run(ci.invoke(LLMConfig(), "sys", "hi", conv_slug="c-raise"))

    assert stamped["log_id"] == 7
    assert stamped["status"] == "failed"


def test_invoke_finalizes_cancelled_on_cancel(
    monkeypatch: pytest.MonkeyPatch, _run_store_state: Any
) -> None:
    """A cancelled turn stamps ``cancelled`` — the run record still gets
    its end-state instead of staying open for the reaper to find."""
    ci = _run_store_state
    import asyncio

    from precis import agentlog

    monkeypatch.setattr(ci, "_run_store", object())
    monkeypatch.setattr(ci, "_open_turn_log", lambda *a: 9)
    stamped: dict[str, Any] = {}

    def fake_close_run(store: Any, lid: int, **kwargs: Any) -> None:
        stamped["log_id"] = lid
        stamped.update(kwargs)

    monkeypatch.setattr(agentlog, "close_run", fake_close_run)

    entered = asyncio.Event()

    async def hang(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    monkeypatch.setattr(ci, "_run_turn", hang)

    from asa_bot.config import LLMConfig

    async def main() -> None:
        task = asyncio.create_task(
            ci.invoke(LLMConfig(), "sys", "hi", conv_slug="c-cx")
        )
        # Cancel must land inside _run_turn: a bare sleep(0) races the
        # to_thread open_run round-trip, and a cancel delivered there
        # propagates before log_id binds — no stamp, KeyError (3.12 leg,
        # run 41194910382).
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(main())

    assert stamped["log_id"] == 9
    assert stamped["status"] == "cancelled"
