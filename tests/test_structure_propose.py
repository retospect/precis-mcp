"""``structure_propose`` job_type — the propose-only LLM edit (bundle).

The claude subprocess is stubbed via the module-level ``AGENT`` hook so the
prompt-build → parse → dry-run → job_result write-back runs offline.
"""

from __future__ import annotations

import json
import re

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


#: One concrete, valid example op per `_OP_VOCAB` entry, in an order that
#: keeps referential integrity (atoms/bonds/measures/eyes exist before they're
#: read) on a fresh two-atom scene. Keyed by op name so the vocab-order test
#: below can detect drift either way: a vocab name with no example here, or
#: an example here for a name the vocab no longer teaches.
_VOCAB_EXAMPLE_SCRIPT: list[dict] = [
    {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
    {"op": "add_atom", "element": "Pd", "frac": [0.3, 0.0, 0.0]},
    {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1, "image": [0, 0, 0]},
    {"op": "displace", "atom": "aPd2", "vector": [0.05, 0, 0], "cartesian": True},
    {"op": "constrain", "atoms": ["aPd1"], "kind": "fixed-x"},
    {
        "op": "eye",
        "name": "site1",
        "atoms": ["aPd1", "aPd2"],
        "reach": 2.0,
        "for": "test",
    },
    {
        "op": "measure",
        "kind": "distance",
        "atoms": ["aPd1", "aPd2"],
        "direction": "min",
        "goal": 2.5,
        "strength": "gauge",
        "for": "test",
    },
    {"op": "unmark", "name": "site1"},
    {"op": "remove_measure", "kind": "distance", "atoms": ["aPd1", "aPd2"]},
    {"op": "remove_bond", "i": "aPd1", "j": "aPd2"},
    {"op": "set_element", "atom": "aPd1", "element": "Ag"},
    {"op": "vacancy", "atom": "aPd2"},
    {"op": "set_cell", "a": 12.0, "b": 12.0, "c": 12.0, "pbc": [True, True, False]},
]


def test_op_vocab_examples_dry_run_against_real_registry():
    """Guard against the `_DSL_CRIB`-class bug: `_OP_VOCAB` names must be real
    ops, taking the params it advertises. Every name the crib teaches gets a
    concrete example applied for real via `apply_ops` — a dead op name (like
    the retired `cursor{...}` that should have been `eye{...}`) or a renamed
    param fails this loudly instead of only failing an agent's dry-run."""
    vocab_names = re.findall(r"(\w+)\{", sp._OP_VOCAB)
    assert vocab_names, "could not parse any op names out of _OP_VOCAB"
    scripted_names = [o["op"] for o in _VOCAB_EXAMPLE_SCRIPT]
    assert set(vocab_names) == set(scripted_names), (
        "_OP_VOCAB and _VOCAB_EXAMPLE_SCRIPT have drifted — every vocab op "
        "needs a working example here (and vice versa)"
    )

    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, False)))
    apply_ops(scene, _VOCAB_EXAMPLE_SCRIPT)  # raises OpError on any failure


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
    vacancy_err = sp.dry_run(scene, [{"op": "vacancy", "atom": "aXe9"}])
    assert vacancy_err is not None
    assert "op error" in vacancy_err
    relax_err = sp.dry_run(scene, [{"op": "relax", "fidelity": "clean"}])
    assert relax_err is not None
    assert "relax" in relax_err


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
        "precis.utils.llm.router.call_claude_agent",
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


def test_dispatch_pins_build_to_sonnet_mid_tier_with_override(seeded, monkeypatch):
    # The structure BUILD step runs on BIG (sonnet) — the round-trip eval
    # showed sonnet ties opus on this mechanical step at ~½ the cost, while
    # catalyst *reasoning* stays FRONTIER=opus. Capture the model that reaches
    # the claude_agent transport to prove the pin (and the revert knob).
    from precis.utils.llm.router import Tier, resolve_model

    store, ref = seeded
    seen: dict = {}
    reply = json.dumps(
        {
            "ops": [{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.55]}],
            "rationale": "x",
        }
    )

    def _capture(*a, **k):
        seen["model"] = k.get("model")
        return AgentResult(final_text=reply, cost_usd=0.0, duration_s=0.0, turns_used=1)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _capture)
    params = {"structure_ref_id": ref.id, "slug": "pp_pd", "instruction": "add O"}

    monkeypatch.delenv("PRECIS_STRUCTURE_PROPOSE_MODEL", raising=False)
    sp._dispatch(_FakeCtx(store, ref.id, params), sp.SPEC)
    assert seen["model"] == resolve_model(Tier.BIG)  # sonnet, the default now
    assert seen["model"] != resolve_model(Tier.FRONTIER)  # NOT opus

    monkeypatch.setenv("PRECIS_STRUCTURE_PROPOSE_MODEL", "claude-opus-4-8")
    sp._dispatch(_FakeCtx(store, ref.id, params), sp.SPEC)
    assert seen["model"] == "claude-opus-4-8"  # explicit override / revert wins


def test_dispatch_marks_invalid_proposal(seeded, monkeypatch):
    store, ref = seeded
    reply = json.dumps(
        {"ops": [{"op": "vacancy", "atom": "aXe99"}], "rationale": "oops"}
    )
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
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
    assert result is not None
    assert result["valid"] is False and "op error" in result["error"]


def test_dispatch_fails_on_unparseable_reply(seeded, monkeypatch):
    store, ref = seeded
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
        lambda *a, **k: AgentResult(
            final_text="I cannot help", cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(
        store, ref.id, {"structure_ref_id": ref.id, "instruction": "do a thing"}
    )
    sp._dispatch(ctx, sp.SPEC)
    assert ctx.status is None and ctx.failure is not None
