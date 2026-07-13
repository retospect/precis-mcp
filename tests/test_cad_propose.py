"""``cad_propose`` job_type — the propose-only LLM edit (ADR 0041 web bundle).

The claude subprocess is stubbed via the module-level ``AGENT`` hook so the
prompt-build → parse → dry-run → job_result write-back runs offline.
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.cad import CadHandler
from precis.utils.claude_agent import AgentResult
from precis.workers.job_types import cad_propose as cp
from precis.workers.job_types import get_job_type, known_job_types

_FLANGE = """
component flange
plate     add  cyl:r25h8
hub_bore  cut  cyl:r8h10    @0,0,-1
"""


# ── registry ─────────────────────────────────────────────────────────────


def test_registered_with_dispatch():
    spec = get_job_type("cad_propose")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "cad_propose" in known_job_types()


# ── pure: prompt / parse / dry-run ───────────────────────────────────────


def test_build_prompt_carries_design_and_instruction():
    prompt = cp.build_prompt("flange", _FLANGE, "widen the plate to r30")
    assert "flange" in prompt and "cyl:r25h8" in prompt
    assert "widen the plate" in prompt
    assert '"source"' in prompt  # the output contract


@pytest.mark.parametrize(
    "text",
    [
        '{"source": "plate add cyl:r30h8", "rationale": "wider"}',
        '```json\n{"source": "plate add cyl:r30h8", "rationale": "x"}\n```',
        'Sure!\n{"source": "plate add cyl:r30h8", "rationale": "y"}',
    ],
)
def test_parse_proposal_tolerant(text):
    p = cp.parse_proposal(text)
    assert isinstance(p["source"], str) and p["source"]
    assert "rationale" in p


def test_parse_proposal_rejects_empty():
    with pytest.raises(ValueError):
        cp.parse_proposal("no json here")
    with pytest.raises(ValueError):
        cp.parse_proposal('{"source": "", "rationale": "nothing"}')


def test_dry_run_valid_and_invalid():
    assert cp.dry_run("plate add cyl:r30h8") is None
    assert "source error" in cp.dry_run("plate frobnicate cyl:r1h1")
    assert "no nodes" in cp.dry_run("# just a comment\n")


# ── dispatch (stubbed agent) ─────────────────────────────────────────────


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


@pytest.fixture
def seeded(store):
    CadHandler(hub=Hub(store=store)).put(id="cp_flange", text=_FLANGE)
    ref = resolve_live_slug_ref(store, kind="cad", id="cp_flange")
    return store, ref


def _agent(reply: str):
    return lambda *a, **k: AgentResult(
        final_text=reply, cost_usd=0.01, duration_s=0.1, turns_used=1
    )


def test_dispatch_writes_valid_proposal(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps(
        {
            "source": "component flange\nplate add cyl:r30h8\nhub_bore cut cyl:r8h10 @0,0,-1",
            "rationale": "widen the plate to r30",
        }
    )
    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _agent(reply))
    ctx = _FakeCtx(
        store,
        ref.id,
        {"cad_ref_id": ref.id, "slug": "cp_flange", "instruction": "widen plate"},
    )
    cp._dispatch(ctx, cp.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert result["valid"] is True
    assert "cyl:r30h8" in result["source"]
    assert ctx.meta_set["proposal_valid"] is True


def test_dispatch_marks_invalid_proposal(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps({"source": "plate frobnicate cyl:r1h1", "rationale": "oops"})
    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _agent(reply))
    ctx = _FakeCtx(store, ref.id, {"cad_ref_id": ref.id, "instruction": "break it"})
    cp._dispatch(ctx, cp.SPEC)
    # A parseable-but-unbuildable proposal still succeeds as a job — surfaced as
    # invalid for the human, not a job failure.
    assert ctx.status == "succeeded"
    result = ctx.result_chunk()
    assert result["valid"] is False and "source error" in result["error"]


def test_dispatch_fails_on_unparseable_reply(seeded, monkeypatch):
    store, ref = seeded
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent", _agent("I cannot help")
    )
    ctx = _FakeCtx(store, ref.id, {"cad_ref_id": ref.id, "instruction": "do a thing"})
    cp._dispatch(ctx, cp.SPEC)
    assert ctx.status is None and ctx.failure is not None
