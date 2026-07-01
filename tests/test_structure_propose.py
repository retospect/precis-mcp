"""``structure_propose`` job_type — the propose-only LLM edit (ADR 0043 bundle).

The claude subprocess is stubbed via the module-level ``AGENT`` hook so the
prompt-build → parse → dry-run → job_result write-back runs offline.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.structure import StructureHandler
from precis.structure.cell import Cell
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis.utils.claude_agent import AgentResult
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import structure_propose as sp

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
        ],
    }
)


def _pd_scene() -> Scene:
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, False)))
    apply_ops(
        scene,
        [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
        ],
    )
    return scene


# ── registry ─────────────────────────────────────────────────────────────


def test_registered_with_dispatch():
    spec = get_job_type("structure_propose")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert "structure_propose" in known_job_types()


# ── pure: prompt / parse / dry-run ───────────────────────────────────────


def test_build_prompt_carries_design_and_instruction():
    prompt = sp.build_prompt("pd_pair", _pd_scene(), "add an O bridging the two Pd")
    assert "pd_pair" in prompt and "aPd1" in prompt and "aPd2" in prompt
    assert "add an O bridging" in prompt
    assert '"ops"' in prompt  # the output contract


@pytest.mark.parametrize(
    "text",
    [
        '{"ops": [{"op": "add_atom", "element": "O", "frac": [0.5,0.5,0.5]}], "rationale": "cap"}',
        '```json\n{"ops": [{"op": "vacancy", "atom": "aPd2"}], "rationale": "x"}\n```',
        'Sure!\n{"ops": [{"op": "vacancy", "atom": "aPd2"}], "rationale": "y"}',
    ],
)
def test_parse_proposal_tolerant(text):
    p = sp.parse_proposal(text)
    assert isinstance(p["ops"], list) and p["ops"]
    assert "rationale" in p


def test_parse_proposal_rejects_empty():
    with pytest.raises(ValueError):
        sp.parse_proposal("no json here")
    with pytest.raises(ValueError):
        sp.parse_proposal('{"ops": [], "rationale": "nothing"}')


def test_dry_run_valid_invalid_and_relax():
    scene = _pd_scene()
    assert (
        sp.dry_run(scene, [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}])
        is None
    )
    assert "op error" in sp.dry_run(scene, [{"op": "vacancy", "atom": "aXe9"}])
    assert "relax" in sp.dry_run(scene, [{"op": "relax", "fidelity": "clean"}])


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
    StructureHandler(hub=Hub(store=store)).put(id="pp_pd", text=_PD)
    ref = resolve_live_slug_ref(store, kind="structure", id="pp_pd")
    return store, ref


def test_dispatch_writes_valid_proposal(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps(
        {
            "ops": [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.55]}],
            "rationale": "bridge the Pd pair with an oxygen",
        }
    )
    monkeypatch.setattr(
        sp,
        "AGENT",
        lambda *a, **k: AgentResult(
            final_text=reply, cost_usd=0.01, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(
        store,
        ref.id,
        {
            "structure_ref_id": ref.id,
            "slug": "pp_pd",
            "instruction": "add an O bridging the Pd",
        },
    )
    sp._dispatch(ctx, sp.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert result["valid"] is True
    assert result["ops"][0]["op"] == "add_atom"
    assert ctx.meta_set["proposed_ops"] == 1


def test_dispatch_marks_invalid_proposal(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps(
        {"ops": [{"op": "vacancy", "atom": "aXe99"}], "rationale": "oops"}
    )
    monkeypatch.setattr(
        sp,
        "AGENT",
        lambda *a, **k: AgentResult(
            final_text=reply, cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(
        store, ref.id, {"structure_ref_id": ref.id, "instruction": "delete a xenon"}
    )
    sp._dispatch(ctx, sp.SPEC)
    # A chemically-wrong-but-parseable proposal still succeeds as a job — it is
    # surfaced as invalid for the human, not a job failure.
    assert ctx.status == "succeeded"
    result = ctx.result_chunk()
    assert result["valid"] is False and "op error" in result["error"]


def test_dispatch_fails_on_unparseable_reply(seeded, monkeypatch):
    store, ref = seeded
    monkeypatch.setattr(
        sp,
        "AGENT",
        lambda *a, **k: AgentResult(
            final_text="I cannot help", cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(
        store, ref.id, {"structure_ref_id": ref.id, "instruction": "do a thing"}
    )
    sp._dispatch(ctx, sp.SPEC)
    assert ctx.status is None and ctx.failure is not None
