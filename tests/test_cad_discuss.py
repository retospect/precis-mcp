"""``cad_discuss`` job_type — the threaded, read-only conversation about a CAD
design (ADR 0041 web bundle).

The claude subprocess is stubbed via the module-level ``AGENT`` hook so the
facts-gather → prompt-build → prose write-back runs offline.
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.cad import CadHandler
from precis.utils.claude_agent import AgentResult
from precis.workers.job_types import cad_discuss as cd
from precis.workers.job_types import get_job_type, known_job_types

# A hub + rim that do NOT touch (no spoke) → the facts block should say so.
_SPLIT = """
component hub
h add cyl:r5h4
component rim
rdisc add cyl:r20h4
rhole cut cyl:r15h6 @0,0,-1
"""


def test_registered_with_dispatch():
    spec = get_job_type("cad_discuss")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "cad_discuss" in known_job_types()


class _FakeCtx:
    def __init__(self, store, ref_id, params):
        self.store = store
        self.ref_id = ref_id
        self.title = "discuss"
        self.meta = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.failure: str | None = None

    def set_status(self, s):
        self.status = s

    def append_chunk(self, kind, text):
        self.chunks.append((kind, text))

    def set_meta(self, **kw):
        pass

    def record_failure(self, msg):
        self.failure = msg

    def is_cancel_requested(self):
        return False

    def result_chunk(self) -> dict | None:
        for kind, text in self.chunks:
            if kind == "job_result":
                return json.loads(text)
        return None


@pytest.fixture
def seeded(store):
    CadHandler(hub=Hub(store=store)).put(id="cd_split", text=_SPLIT)
    ref = resolve_live_slug_ref(store, kind="cad", id="cd_split")
    return store, ref


def _agent_capture(reply: str, sink: dict):
    def _fn(prompt, *a, **k):
        sink["prompt"] = prompt
        return AgentResult(
            final_text=reply, cost_usd=0.01, duration_s=0.1, turns_used=1
        )

    return _fn


def test_dispatch_writes_prose_answer_grounded_in_facts(seeded, monkeypatch):
    store, ref = seeded
    sink: dict = {}
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
        _agent_capture("The hub and rim don't touch; add a spoke.", sink),
    )
    ctx = _FakeCtx(
        store,
        ref.id,
        {
            "cad_ref_id": ref.id,
            "slug": "cd_split",
            "instruction": "why not functional?",
        },
    )
    cd._dispatch(ctx, cd.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert "spoke" in result["answer"]
    assert result["instruction"] == "why not functional?"
    # the prompt is grounded in the measured connectivity facts
    assert "SEPARATE bodies" in sink["prompt"]
    assert "cd_split" in sink["prompt"] and "why not functional?" in sink["prompt"]
    # …and in real per-feature bounds + the coordinate convention, so the model
    # doesn't guess where a part's zero is (the reported-bug fix).
    assert "Per-feature world bounds" in sink["prompt"]
    assert "Coordinates:" in sink["prompt"]
    # the rim disc is r20 h4 at origin → z spans 0..4 (base-at-0, not centred)
    assert "rdisc [rim]" in sink["prompt"]
    assert "z[0..4]" in sink["prompt"]


def test_dispatch_fails_on_empty_answer(seeded, monkeypatch):
    store, ref = seeded
    sink: dict = {}
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent", _agent_capture("   ", sink)
    )
    ctx = _FakeCtx(store, ref.id, {"cad_ref_id": ref.id, "instruction": "hello?"})
    cd._dispatch(ctx, cd.SPEC)
    assert ctx.status is None and ctx.failure is not None


def test_build_prompt_includes_prior_turns():
    prompt = cd.build_prompt(
        "wheel",
        "component hub\nh add cyl:r5h4",
        "Connectivity: ONE connected solid.",
        "and the rim?",
        [{"instruction": "what is this?", "answer": "a wheel hub."}],
    )
    assert "Conversation so far" in prompt
    assert "what is this?" in prompt and "a wheel hub." in prompt
    assert "and the rim?" in prompt
