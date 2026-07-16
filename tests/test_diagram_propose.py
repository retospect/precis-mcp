"""``diagram_propose`` job_type — one autonomous diagram turn (ADR 0057, slice 5).

The model call (the figure/mermaid turn shim → the LLM router) is stubbed by
monkeypatching ``precis.utils.llm.router.dispatch``, so the resolve → compose
seed message → run turn (mutates the diagram + bindings) → job_result path runs
offline. The turn loop itself is the shared core exercised by the figure/mermaid
suites."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from precis.dispatch import Hub
from precis.handlers.figure import FigureHandler
from precis.workers.job_types import diagram_propose as dp
from precis.workers.job_types import get_job_type, known_job_types

_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<circle id="hook" cx="50" cy="50" r="20"/></svg>'
)


@pytest.fixture(autouse=True)
def _clear_agentic_env(monkeypatch):
    """Each test owns its L3 gate: default to single-shot by clearing both the
    override and the MCP-config auto-trigger, so a CI env that happens to set
    ``PRECIS_MCP_CONFIG`` can't silently flip the dispatch tests to agentic."""
    monkeypatch.delenv("PRECIS_DIAGRAM_AGENTIC", raising=False)
    monkeypatch.delenv("PRECIS_MCP_CONFIG", raising=False)


class _FakeCtx:
    def __init__(self, store, ref_id, params):
        self.store = store
        self.ref_id = ref_id
        self.title = "propose"
        self.meta = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.meta_set: dict = {}
        self.failure: str | None = None

    def set_status(self, s):
        self.status = s

    def append_chunk(self, kind, text):
        self.chunks.append((kind, text))

    def set_meta(self, **kw):
        self.meta_set.update(kw)

    def record_failure(self, msg):
        self.failure = msg

    def is_cancel_requested(self):
        return False

    def result_chunk(self) -> dict | None:
        for kind, text in self.chunks:
            if kind == "job_result":
                return json.loads(text)
        return None


# ── registry ─────────────────────────────────────────────────────────────


def test_registered_with_dispatch() -> None:
    spec = get_job_type("diagram_propose")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "diagram_propose" in known_job_types()


# ── compose_message (pure) ─────────────────────────────────────────────────


def _target_chunk(store) -> str:
    proj = store.insert_ref(kind="todo", slug=None, title="p").id
    src, _t = store.create_draft(name="parts", title="Parts", project_ref_id=proj)
    store.add_chunks(ref_id=src.id, chunk_kind="paragraph", text="a stainless hook")
    return store.reading_order(src.id)[1].dc


def test_compose_message_inlines_seed_chunks(store) -> None:
    h = _target_chunk(store)
    msg = dp.compose_message(store, "draw the hook", [h])
    assert "draw the hook" in msg
    assert "Reading material" in msg
    assert h in msg
    assert "a stainless hook" in msg  # the chunk body inlined


def test_compose_message_without_seeds_is_just_the_instruction(store) -> None:
    assert dp.compose_message(store, "draw it", []) == "draw it"


# ── dispatch (stubbed model) ────────────────────────────────────────────────


def _stub_dispatch(reply: dict):
    """A fake router dispatch returning the turn model's JSON in ``.data``."""
    return lambda _req: SimpleNamespace(error=None, data=reply, text=json.dumps(reply))


@pytest.fixture
def figure_and_seed(store):
    FigureHandler(hub=Hub(store=store)).put(id="hookfig", title="Hook")
    ref = store.get_ref(kind="figure", id="hookfig")
    return ref, _target_chunk(store)


def _source_chunk_id(store, ref_id):
    for c in store.reading_order(ref_id, kind="figure"):
        if c.chunk_kind == "figure_node":
            return c.chunk_id
    raise AssertionError("no figure_node")


def test_dispatch_builds_figure_and_binds(store, figure_and_seed, monkeypatch) -> None:
    ref, h = figure_and_seed
    reply = {
        "reply": "drew the hook",
        "svg": _SVG,
        "links": [{"element": "hook", "target": h, "relation": "depicts"}],
    }
    monkeypatch.setattr("precis.utils.llm.router.dispatch", _stub_dispatch(reply))

    ctx = _FakeCtx(
        store,
        ref.id,
        {
            "kind": "figure",
            "ref_id": ref.id,
            "instruction": "draw the deck hook",
            "seeds": [h],
        },
    )
    dp._dispatch(ctx, dp.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert result["changed"] is True
    assert result["reply"] == "drew the hook"
    assert {b["element"] for b in result["bindings"]} == {"hook"}

    # the diagram was actually mutated + the binding persisted
    node = _source_chunk_id(store, ref.id)
    src_text = next(
        c.text
        for c in store.reading_order(ref.id, kind="figure")
        if c.chunk_kind == "figure_node"
    )
    assert 'id="hook"' in src_text  # source rewritten in place
    got = {(b["element"], b["handle"]) for b in store.element_bindings(node)}
    assert got == {("hook", h)}


def test_dispatch_missing_diagram_fails(store, monkeypatch) -> None:
    ctx = _FakeCtx(
        store, 999999, {"kind": "figure", "ref_id": 999999, "instruction": "x"}
    )
    dp._dispatch(ctx, dp.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "not found" in ctx.failure


def test_dispatch_rejects_bad_kind(store, monkeypatch) -> None:
    ctx = _FakeCtx(store, 1, {"kind": "banana", "ref_id": 1, "instruction": "x"})
    dp._dispatch(ctx, dp.SPEC)
    assert ctx.failure is not None and "unsupported kind" in ctx.failure


# ── the L3 agentic gate ────────────────────────────────────────────────────


def test_agentic_gate_off_by_default() -> None:
    # env cleared by the autouse fixture: no MCP config, no override → single-shot
    assert dp._agentic_enabled() is False


def test_agentic_gate_explicit_override(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_DIAGRAM_AGENTIC", "1")
    assert dp._agentic_enabled() is True
    monkeypatch.setenv("PRECIS_DIAGRAM_AGENTIC", "0")
    assert dp._agentic_enabled() is False


def test_agentic_gate_auto_on_mcp_config(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / "mcp.json"
    cfg.write_text("{}")
    monkeypatch.setenv("PRECIS_MCP_CONFIG", str(cfg))
    assert dp._agentic_enabled() is True
    # override still wins over the auto-trigger
    monkeypatch.setenv("PRECIS_DIAGRAM_AGENTIC", "0")
    assert dp._agentic_enabled() is False


def test_dispatch_agentic_runs_tool_using_fn(
    store, figure_and_seed, monkeypatch
) -> None:
    """With the gate on, the turn is driven by the agentic drawer: the seam sees
    ``tools_needed=True`` and the outcome is recorded as agentic."""
    ref, h = figure_and_seed
    reply = {
        "reply": "drew it with tools",
        "svg": _SVG,
        "links": [{"element": "hook", "target": h, "relation": "depicts"}],
    }
    captured: dict = {}

    def _fake_dispatch(req):
        captured["tools_needed"] = req.tools_needed
        captured["source"] = req.source
        return SimpleNamespace(error=None, data=reply, text=json.dumps(reply))

    monkeypatch.setenv("PRECIS_DIAGRAM_AGENTIC", "1")
    monkeypatch.setattr("precis.utils.llm.router.dispatch", _fake_dispatch)

    ctx = _FakeCtx(
        store,
        ref.id,
        {"kind": "figure", "ref_id": ref.id, "instruction": "draw the deck hook"},
    )
    dp._dispatch(ctx, dp.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    assert captured["tools_needed"] is True
    assert captured["source"] == "diagram_propose:figure"
    assert ctx.meta_set.get("agentic") is True
    assert any("[agentic]" in text for kind, text in ctx.chunks if kind == "job_event")
    result = ctx.result_chunk()
    assert result is not None and result["changed"] is True
    assert result["reply"] == "drew it with tools"
