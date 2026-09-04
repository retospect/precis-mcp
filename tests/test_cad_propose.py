"""``cad_propose`` job_type — the propose-only LLM edit (web bundle).

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
    err, warnings = cp.dry_run("plate add cyl:r30h8")
    assert err is None and warnings == []
    frobnicate_err, _ = cp.dry_run("plate frobnicate cyl:r1h1")
    assert frobnicate_err is not None
    assert "source error" in frobnicate_err
    comment_err, _ = cp.dry_run("# just a comment\n")
    assert comment_err is not None
    assert "no nodes" in comment_err


# ── geometry lint: disconnection / empty component / interference ────────

#: The backlog repro: spokes too short to bridge the hub-to-rim gap — three
#: separate bodies land as three separate contact groups.
_BROKEN_WHEEL = """
component hub
body    add  cyl:r12h10
component rim
outer   add  cyl:r40h10
inner   cut  cyl:r34h12    @0,0,-1
component spokes
bar     add  box:w8d8h10   @26,0,0
"""

#: Same wheel, spokes widened to actually bridge hub (r12) to rim's inner
#: wall (r34) — the corrected design should read as one connected solid.
_FIXED_WHEEL = """
component hub
body    add  cyl:r12h10
component rim
outer   add  cyl:r40h10
inner   cut  cyl:r34h12    @0,0,-1
component spokes
bar     add  box:w26d8h10  @23,0,0
"""


def test_dry_run_flags_disconnected_assembly():
    err, warnings = cp.dry_run(_BROKEN_WHEEL)
    assert err is not None
    assert "disconnected" in err
    # names every floating body, not just one
    assert "hub" in err and "rim" in err and "spokes" in err
    assert warnings == []


def test_dry_run_corrected_spokes_are_valid():
    err, _warnings = cp.dry_run(_FIXED_WHEEL)
    assert err is None


def test_dry_run_flags_empty_component():
    # the cut cylinder (r20h20) fully engulfs the r10h10 base — nothing left.
    source = "component plate\nbody   add cyl:r10h10\ncutter cut cyl:r20h20 @0,0,-5"
    err, _warnings = cp.dry_run(source)
    assert err is not None
    assert "plate" in err
    assert "volume" in err


def test_dry_run_interference_is_a_warning_not_invalid():
    # sleeve sits fully inside shaft — deep interference, but that can be an
    # intentional press fit, so it must not invalidate the proposal.
    source = (
        "component shaft\nshaft_body add cyl:r10h20\n"
        "component sleeve\nsleeve_body add cyl:r9h20"
    )
    err, warnings = cp.dry_run(source)
    assert err is None
    assert warnings and any("interpenetrate" in w for w in warnings)
    assert "shaft" in warnings[0] and "sleeve" in warnings[0]


def test_dry_run_single_component_skips_disconnection_check():
    # one part only — nothing to be disconnected FROM, but volume still lints.
    err, warnings = cp.dry_run("solo add cyl:r10h10")
    assert err is None and warnings == []


def test_dry_run_lone_floater_reads_as_does_not_touch():
    # exactly two contact groups with one lone part — the message reads
    # "X does not touch {the rest}", not the generic N-bodies split.
    source = (
        "component base\nslab add box:w40d40h5\n"
        "component post\npin  add cyl:r3h10 @10,10,4\n"  # overlaps the slab
        "component floater\ncube add box:w5d5h5 @100,100,0\n"
    )
    err, _warnings = cp.dry_run(source)
    assert err is not None
    # orientation matters: the LONE part leads, the connected rest follows.
    assert err.startswith("disconnected: {floater} does not touch")
    assert "base" in err and "post" in err


def test_dry_run_skips_component_whose_volume_cannot_be_computed(monkeypatch):
    # an unboundable/degenerate component expression must be skipped by the
    # empty-volume lint, not crash the dry run or flag the design.
    def _boom(*a, **kw):
        raise ValueError("unbounded expression")

    monkeypatch.setattr(cp, "cad_volume", _boom)
    err, warnings = cp.dry_run("solo add cyl:r10h10")
    assert err is None and warnings == []


def test_dry_run_reports_kernel_build_error(monkeypatch):
    def _boom(spec, **_kw):
        raise RuntimeError("kernel exploded")

    monkeypatch.setattr(cp, "build_design", _boom)
    err, _warnings = cp.dry_run("solo add cyl:r10h10")
    assert err is not None
    assert "build error" in err and "kernel exploded" in err


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
    assert result["warnings"] == []
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
    assert result is not None
    assert result["valid"] is False and "source error" in result["error"]
    assert result["warnings"] == []


def test_dispatch_marks_disconnected_proposal_invalid(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps({"source": _BROKEN_WHEEL, "rationale": "add a wheel"})
    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _agent(reply))
    ctx = _FakeCtx(store, ref.id, {"cad_ref_id": ref.id, "instruction": "add a wheel"})
    cp._dispatch(ctx, cp.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert result["valid"] is False
    assert "disconnected" in result["error"]
    # the job_summary line surfaces the same verdict for the human skimming the log
    summary = next(text for kind, text in ctx.chunks if kind == "job_summary")
    assert "INVALID" in summary


def test_dispatch_fails_on_unparseable_reply(seeded, monkeypatch):
    store, ref = seeded
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent", _agent("I cannot help")
    )
    ctx = _FakeCtx(store, ref.id, {"cad_ref_id": ref.id, "instruction": "do a thing"})
    cp._dispatch(ctx, cp.SPEC)
    assert ctx.status is None and ctx.failure is not None
