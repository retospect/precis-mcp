"""``se_propose_atomic`` job_type — the propose-only LLM fragment fill for
one atomic-mode ``se`` block (:mod:`precis_se.atomic.propose`).

nm's ``tests/test_nm_propose.py`` ported onto se by the nm→se merge
(docs/backlog/nm-se-merge.md "Job/service rename"): same prompt-build →
parse → dry-run → job_result write-back, retargeted at
:class:`~precis_se.handler.SeHandler` and the renamed spec. Two things
differ from the nm originals, deliberately, the same two
``tests/test_se_atomic_bind.py`` documents: envelopes are canonical metres
(``sphere:r3e-10``, not nm's required-unit ``sphere:r3Å``), and a bond
connect states ``kind='bond'`` explicitly.

A plugin job_type (entry-point declared in pyproject, group
``precis.job_types``) — no entry-point discovery at test time (the
``retrosynth``/``test_route_plugin.py::register_retrosynth`` precedent):
this module injects :data:`precis_se.atomic.propose.SPEC` into the registry
directly.

The claude subprocess is stubbed via the router's ``call_claude_agent`` seam
so the whole path runs offline, same as ``tests/test_structure_propose.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis.workers.job_types as jt
import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom
from precis.structure.scene import Scene as StructScene
from precis.utils.claude_agent import AgentResult
from precis.utils.llm.router import Tier, resolve_model
from precis.workers.job_types import get_job_type
from precis_se import persist
from precis_se.atomic import propose as sej
from precis_se.handler import SeHandler
from precis_se.ops import SeTree

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def se_handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


@pytest.fixture
def register_se_propose_atomic() -> Any:
    """Inject the ``se_propose_atomic`` job_type into the registry for the
    test (mirrors ``test_route_plugin.py::register_retrosynth`` — no
    entry-point discovery at test time); remove it after."""
    jt._REGISTRY["se_propose_atomic"] = sej.SPEC
    yield
    jt._REGISTRY.pop("se_propose_atomic", None)


_ROTAXANE_OPS = [
    {
        "op": "add_block",
        "name": "hub",
        "envelope": "sphere:r3e-10",
        "desc": "stopper hub, threads the axle",
        "use": "stopper",
    },
    {
        "op": "add_block",
        "name": "axle",
        "envelope": "cyl:r2e-10h2e-09",
        "desc": "threading rod",
    },
    {
        "op": "add_port",
        "block": "hub",
        "name": "cap",
        "roles": ["covalent"],
        "expected_element": "C",
    },
    {"op": "add_port", "block": "axle", "name": "tip", "roles": ["covalent"]},
    {
        "op": "connect",
        "a": "hub.cap",
        "b": "axle.tip",
        "kind": "bond",
        # se vets objectives against a registered vocabulary
        # (:func:`precis_se.joints.validate_objectives`), so the free-form
        # ``{'distance': 1.5}`` nm's own suite used isn't storable here —
        # ``role`` is the atomic-mode key that is.
        "objectives": {"role": "covalent"},
    },
]


def _seeded_tree(se_handler: SeHandler) -> SeTree:
    se_handler.put(id="rotaxane1", text=json.dumps({"ops": _ROTAXANE_OPS}))
    ref = se_handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    return persist.load_tree(se_handler.store, ref.id)


# ── registry ─────────────────────────────────────────────────────────────


def test_registered_via_injection(register_se_propose_atomic: Any) -> None:
    # No entry-point discovery at test time (module docstring) — this only
    # exercises the `_REGISTRY` lookup `get_job_type` consults first, the
    # same shape `test_route_plugin.py::register_retrosynth` proves for
    # `retrosynth`. `known_job_types()` (a separate, real-discovery-only
    # roster) is not asserted here for the same reason.
    spec = get_job_type("se_propose_atomic")
    assert spec is not None and spec.dispatch is not None
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert spec.requires == frozenset({"claude_bin"})


def test_the_job_name_is_not_the_reserved_whole_design_proposer() -> None:
    """``se_propose`` is reserved for se-kind.md's unshipped whole-design
    proposer; the merged nm job is the atomic-mode *fragment* fill, so it
    must not squat that name (nm-se-merge.md "Job/service rename")."""
    assert sej.SPEC.name == "se_propose_atomic"


# ── pure: prompt ────────────────────────────────────────────────────────


def test_build_prompt_carries_block_ports_objectives_and_steer(
    se_handler: SeHandler,
) -> None:
    tree = _seeded_tree(se_handler)
    findings: list[Any] = []
    prompt = sej.build_prompt(
        "rotaxane1", tree, "hub", tree.blocks["hub"], findings, "prefer an aromatic cap"
    )
    assert "'hub'" in prompt
    assert "sphere:r3e-10" in prompt  # target envelope, canonical metres
    assert "cap" in prompt and "expected=C" in prompt  # port roster
    assert "hub.cap" in prompt and "axle.tip" in prompt  # objective vectors
    assert '"role": "covalent"' in prompt
    assert "prefer an aromatic cap" in prompt
    assert '"port_atom_map"' in prompt  # the output contract
    assert "'relax'" in prompt  # the relax prohibition is stated
    # units-cutover: the envelope is bare canonical metres, but every op
    # coordinate/distance is Å — neither convention was ever stated
    # (prompt-surface audit item 7); both must be spelled out.
    assert "bare canonical metres" in prompt
    assert "is Å, never metres" in prompt


def test_build_prompt_default_steer_when_absent(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    prompt = sej.build_prompt("rotaxane1", tree, "hub", tree.blocks["hub"], [], None)
    assert "own chemical judgment" in prompt


def test_design_findings_concatenates_se_and_atomic_checks(
    se_handler: SeHandler,
) -> None:
    """The context list the prompt shows is BOTH validators, the same
    concatenation ``view='validate'`` renders — an atomic-mode proposer
    that only saw se's own findings would be blind to the chemistry ones
    (and vice versa)."""
    tree = _seeded_tree(se_handler)
    # Hand-corrupt the binding: ``dangling_binding`` is an atomic-only rule
    # (precis_se.atomic.validate), while ``cross_scale_unverifiable`` on
    # this nanoscale design is se's own (precis_se.validate) — one call has
    # to surface both.
    hub = tree.blocks["hub"]
    hub.bound_kind = "structure"
    hub.bound = "gone-missing"
    hub.ports["cap"].bound_design = "gone-missing"
    hub.ports["cap"].bound_atom = "aC1"
    rules = {f.rule for f in sej.design_findings(se_handler.store, tree)}
    assert "dangling_binding" in rules
    assert "cross_scale_unverifiable" in rules


# ── pure: parse ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        json.dumps(
            {
                "fragment": {"smiles": "c1ccccc1"},
                "ops": [{"op": "ring", "element": "C", "n": 6, "aromatic": True}],
                "port_atom_map": {"cap": "aC1"},
                "rationale": "an aromatic cap",
            }
        ),
        "```json\n"
        + json.dumps(
            {
                "fragment": {"structure_slug": "known-cap", "note": "reuse"},
                "ops": [{"op": "ring", "element": "C", "n": 6, "aromatic": True}],
                "port_atom_map": {"cap": "aC1"},
            }
        )
        + "\n```",
        "Sure, here you go:\n"
        + json.dumps(
            {
                "fragment": {"smiles": "CC"},
                "ops": [{"op": "ring", "element": "C", "n": 3}],
                "port_atom_map": {"cap": "aC1"},
                "rationale": "x",
            }
        ),
    ],
)
def test_parse_proposal_tolerant(text: str) -> None:
    p = sej.parse_proposal(text)
    assert isinstance(p["ops"], list) and p["ops"]
    assert isinstance(p["port_atom_map"], dict) and p["port_atom_map"]
    assert "fragment" in p and "rationale" in p


def test_parse_proposal_rejects_missing_pieces() -> None:
    with pytest.raises(ValueError, match="no JSON object"):
        sej.parse_proposal("no json here")
    with pytest.raises(ValueError, match="fragment"):
        sej.parse_proposal(
            json.dumps({"ops": [{"op": "ring"}], "port_atom_map": {"a": "b"}})
        )
    with pytest.raises(ValueError, match="ops"):
        sej.parse_proposal(
            json.dumps(
                {
                    "fragment": {"smiles": "CC"},
                    "ops": [],
                    "port_atom_map": {"a": "b"},
                }
            )
        )
    with pytest.raises(ValueError, match="port_atom_map"):
        sej.parse_proposal(
            json.dumps(
                {
                    "fragment": {"smiles": "CC"},
                    "ops": [{"op": "ring", "element": "C", "n": 3}],
                    "port_atom_map": {},
                }
            )
        )


# ── pure: dry_run ────────────────────────────────────────────────────────


def test_dry_run_valid_ring_proposal(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    ops = [{"op": "ring", "element": "C", "n": 6, "aromatic": True}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is None
    # A symmetric aromatic hexagon at its own ideal 120 deg has no vsepr
    # strain and comfortably fits sphere:r3e-10 by default bond length.
    assert warnings == []


def test_dry_run_unknown_block() -> None:
    tree = SeTree()
    err, warnings = sej.dry_run(tree, "nope", [], {})
    assert err is not None and "no such block" in err
    assert warnings == []


def test_dry_run_rejects_instance_target(se_handler: SeHandler) -> None:
    se_handler.put(
        id="withinst",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "core", "envelope": "sphere:r3e-10"},
                    {"op": "instance_block", "template": "core", "name": "core2"},
                ]
            }
        ),
    )
    ref = se_handler.store.get_ref(kind="se", id="withinst")
    assert ref is not None
    tree = persist.load_tree(se_handler.store, ref.id)
    err, warnings = sej.dry_run(
        tree, "core2", [{"op": "ring", "element": "C", "n": 6}], {}
    )
    assert err is not None and "instance" in err
    assert warnings == []


def test_dry_run_rejects_relax_op(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    ops: list[dict[str, Any]] = [
        {"op": "ring", "element": "C", "n": 6, "aromatic": True},
        {"op": "relax", "fidelity": "clean"},
    ]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is not None and "relax" in err
    assert warnings == []


def test_dry_run_rejects_bad_op(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    err, warnings = sej.dry_run(tree, "hub", [{"op": "vacancy", "atom": "aXe9"}], {})
    assert err is not None and "op error" in err


def test_dry_run_rejects_unknown_port(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    ops = [{"op": "ring", "element": "C", "n": 6, "aromatic": True}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"nope": "aC1"})
    assert err is not None and "no such port" in err


def test_dry_run_rejects_unknown_atom(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    ops = [{"op": "ring", "element": "C", "n": 6, "aromatic": True}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aXe99"})
    assert err is not None and "no such atom" in err


def test_dry_run_rejects_element_mismatch(se_handler: SeHandler) -> None:
    # port 'cap' declares expected_element='C'; ring of N atoms mismatches.
    tree = _seeded_tree(se_handler)
    ops = [{"op": "ring", "element": "N", "n": 6, "aromatic": True}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aN1"})
    assert err is not None and "expects element" in err


def test_dry_run_rejects_structure_validate_error(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    # Two carbons sub-covalent-close together — atomic overlap, validate's
    # hard-reject gate.
    ops = [
        {"op": "add_atom", "element": "C", "cart": [0.0, 0.0, 0.0], "label": "aC1"},
        {"op": "add_atom", "element": "C", "cart": [0.1, 0.0, 0.0], "label": "aC2"},
    ]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is not None and "structure validate" in err


def test_dry_run_surfaces_warnings_without_failing(se_handler: SeHandler) -> None:
    tree = _seeded_tree(se_handler)
    # A 3-membered carbon ring is legal chemistry but strained — vsepr's
    # small_ring/angle_strain advisories fire, warn tier only.
    ops = [{"op": "ring", "element": "C", "n": 3, "aromatic": False}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is None
    assert warnings  # non-empty, but still a valid proposal


def test_dry_run_envelope_fit_warning_does_not_fail(se_handler: SeHandler) -> None:
    # hub's envelope is sphere:r3e-10 (3 Å). Two ordinary-bond-length
    # hexagons far apart from each other keep every individual bond legal
    # (no bond_too_long finding) while the FRAGMENT as a whole — the two
    # rings' combined centroid vs. either ring's actual position — spans
    # well past the envelope + margin. This is exactly the "no real bind
    # pose exists yet" gap the fit-check's docstring names: a proposal this
    # spread out is a bad fit for a small envelope, but it's still
    # warn-only.
    tree = _seeded_tree(se_handler)
    ops = [
        {"op": "ring", "element": "C", "n": 6, "aromatic": True},
        {
            "op": "ring",
            "element": "C",
            "n": 6,
            "aromatic": True,
            "center": [20.0, 0.0, 0.0],
        },
    ]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is None
    assert any("envelope_fit" in w for w in warnings)


def test_envelope_fit_warning_reports_protrusion_not_sdf_plus_margin(
    se_handler: SeHandler,
) -> None:
    """The reported figure is the PROTRUSION past the vdW allowance (worst
    SDF minus margin) — the same quantity :func:`precis_se.atomic.validate.
    envelope_fit` reports at bind time (module docstring: "the propose-time
    warning and the later bind-time finding ... agree instead of differing
    by exactly one margin"), never worst-SDF-PLUS-margin, which would
    silently inflate the number."""
    tree = _seeded_tree(se_handler)
    node = tree.blocks["hub"]  # sphere:r3e-10 — a 3 Å envelope radius
    cell = Cell.from_lengths_angles(30.0, 30.0, 30.0, pbc=(False, False, False))
    scene = StructScene(cell=cell)
    scene.atoms["a"] = Atom(
        label="a", element="C", frac=cell.cart_to_frac(np.array([0.0, 0.0, 0.0]))
    )
    scene.atoms["b"] = Atom(
        label="b", element="C", frac=cell.cart_to_frac(np.array([16.0, 0.0, 0.0]))
    )
    # centroid recentres to (8,0,0) Å, so both atoms sit 8 Å from center:
    # SDF against the 3 Å sphere is 5 Å, 3.3 Å past the 1.7 Å vdW margin.
    warning = sej._envelope_fit_warnings(tree, node, scene)
    assert warning is not None
    assert "3.30 Å" in warning
    assert "6.70 Å" not in warning  # the (sdf + margin) mutant's figure


def test_dry_run_unbounded_envelope_skips_fit_check(se_handler: SeHandler) -> None:
    # A chamfer half-space builds fine as a cad primitive (since the chamfer
    # authoring slice) but is unbounded — it has no centroid to align the
    # fragment to, so the fit check must skip it gracefully (same posture as
    # a bad config), never emit a NaN-derived warning or crash.
    tree = _seeded_tree(se_handler)
    tree.blocks["hub"].envelope = "chamfer:1x45"
    ops = [{"op": "ring", "element": "C", "n": 6, "aromatic": True}]
    err, warnings = sej.dry_run(tree, "hub", ops, {"cap": "aC1"})
    assert err is None
    assert not any("envelope_fit" in w for w in warnings)


# ── dispatch (stubbed agent) ─────────────────────────────────────────────


class _FakeCtx:
    def __init__(self, store: Store, ref_id: int, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = ref_id
        self.title = "propose"
        self.meta = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.status: str | None = None
        self.meta_set: dict[str, Any] = {}
        self.failure: str | None = None

    def set_status(self, s: str) -> None:
        self.status = s

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, msg: str) -> None:
        self.failure = msg

    def is_cancel_requested(self) -> bool:
        return False

    def result_chunk(self) -> dict[str, Any] | None:
        for kind, text in self.chunks:
            if kind == "job_result":
                return json.loads(text)
        return None


@pytest.fixture
def seeded_ctx(se_handler: SeHandler) -> tuple[Store, int]:
    _seeded_tree(se_handler)
    ref = se_handler.store.get_ref(kind="se", id="rotaxane1")
    assert ref is not None
    return se_handler.store, ref.id


def test_dispatch_writes_valid_proposal(
    seeded_ctx: tuple[Store, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ref_id = seeded_ctx
    reply = json.dumps(
        {
            "fragment": {"smiles": "c1ccccc1"},
            "ops": [{"op": "ring", "element": "C", "n": 6, "aromatic": True}],
            "port_atom_map": {"cap": "aC1"},
            "rationale": "a benzene cap fits the stopper hub",
        }
    )
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
        lambda *a, **k: AgentResult(
            final_text=reply, cost_usd=0.02, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(
        store, ref_id, {"se_ref_id": ref_id, "slug": "rotaxane1", "block": "hub"}
    )
    sej._dispatch(ctx, sej.SPEC)

    assert ctx.status == "succeeded" and ctx.failure is None
    result = ctx.result_chunk()
    assert result is not None
    assert result["valid"] is True
    assert result["port_atom_map"] == {"cap": "aC1"}
    assert result["block"] == "hub"
    assert result["se_ref_id"] == ref_id
    assert ctx.meta_set["proposal_valid"] is True
    assert ctx.meta_set["proposed_ops"] == 1


def test_dispatch_pins_frontier_tier_with_override(
    seeded_ctx: tuple[Store, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ref_id = seeded_ctx
    seen: dict[str, Any] = {}
    reply = json.dumps(
        {
            "fragment": {"smiles": "c1ccccc1"},
            "ops": [{"op": "ring", "element": "C", "n": 6, "aromatic": True}],
            "port_atom_map": {"cap": "aC1"},
            "rationale": "x",
        }
    )

    def _capture(*a: Any, **k: Any) -> AgentResult:
        seen["model"] = k.get("model")
        return AgentResult(final_text=reply, cost_usd=0.0, duration_s=0.0, turns_used=1)

    monkeypatch.setattr("precis.utils.llm.router.call_claude_agent", _capture)
    params = {"se_ref_id": ref_id, "slug": "rotaxane1", "block": "hub"}

    monkeypatch.delenv("PRECIS_SE_PROPOSE_ATOMIC_MODEL", raising=False)
    sej._dispatch(_FakeCtx(store, ref_id, params), sej.SPEC)
    assert seen["model"] == resolve_model(Tier.FRONTIER)  # opus, the default

    monkeypatch.setenv("PRECIS_SE_PROPOSE_ATOMIC_MODEL", "claude-sonnet-4-8")
    sej._dispatch(_FakeCtx(store, ref_id, params), sej.SPEC)
    assert seen["model"] == "claude-sonnet-4-8"  # explicit override wins


def test_dispatch_is_tool_less(
    seeded_ctx: tuple[Store, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propose-only means the agent CANNOT act — pinned at the LlmRequest
    boundary, not below it.

    The other dispatch tests stub ``call_claude_agent``, which sits *under*
    the request construction, so a regression that wired up ``mcp_config``
    (handing the proposing agent real tools) would sail past every one of
    them. This asserts on the request the job actually builds.
    """
    captured: dict[str, Any] = {}
    reply = json.dumps(
        {
            "fragment": {"smiles": "c1ccccc1"},
            "ops": [{"op": "ring", "element": "C", "n": 6, "aromatic": True}],
            "port_atom_map": {"cap": "aC1"},
            "rationale": "x",
        }
    )

    class _Res:
        error = None
        text = reply

    def _capture_request(req: Any) -> Any:
        captured["req"] = req
        return _Res()

    store, ref_id = seeded_ctx
    monkeypatch.setattr(sej, "route", _capture_request)
    sej._dispatch(
        _FakeCtx(
            store, ref_id, {"se_ref_id": ref_id, "slug": "rotaxane1", "block": "hub"}
        ),
        sej.SPEC,
    )

    req = captured["req"]
    assert req.mcp_config is None, (
        "se_propose_atomic must never hand the agent MCP tools"
    )
    assert "WebFetch" in req.disallowed_tools
    assert "WebSearch" in req.disallowed_tools


def test_dispatch_marks_invalid_proposal(
    seeded_ctx: tuple[Store, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ref_id = seeded_ctx
    # Wrong element for the 'cap' port (expects C) -> dry_run rejects it.
    reply = json.dumps(
        {
            "fragment": {"smiles": "N"},
            "ops": [{"op": "ring", "element": "N", "n": 6, "aromatic": True}],
            "port_atom_map": {"cap": "aN1"},
            "rationale": "oops wrong element",
        }
    )
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
        lambda *a, **k: AgentResult(
            final_text=reply, cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(store, ref_id, {"se_ref_id": ref_id, "block": "hub"})
    sej._dispatch(ctx, sej.SPEC)
    # A chemically-wrong-but-parseable proposal still succeeds as a job — it
    # is surfaced as invalid for the human, not a job failure.
    assert ctx.status == "succeeded"
    result = ctx.result_chunk()
    assert result is not None
    assert result["valid"] is False and "expects element" in result["error"]


def test_dispatch_fails_on_unparseable_reply(
    seeded_ctx: tuple[Store, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, ref_id = seeded_ctx
    monkeypatch.setattr(
        "precis.utils.llm.router.call_claude_agent",
        lambda *a, **k: AgentResult(
            final_text="I cannot help", cost_usd=0.0, duration_s=0.1, turns_used=1
        ),
    )
    ctx = _FakeCtx(store, ref_id, {"se_ref_id": ref_id, "block": "hub"})
    sej._dispatch(ctx, sej.SPEC)
    assert ctx.status is None and ctx.failure is not None


def test_dispatch_fails_on_unknown_block(seeded_ctx: tuple[Store, int]) -> None:
    store, ref_id = seeded_ctx
    ctx = _FakeCtx(store, ref_id, {"se_ref_id": ref_id, "block": "nope"})
    sej._dispatch(ctx, sej.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "no such block" in ctx.failure


def test_dispatch_fails_on_the_old_nm_param_name(
    seeded_ctx: tuple[Store, int],
) -> None:
    """The param renamed with the job (``nm_ref_id`` → ``se_ref_id``). A
    stale caller must fail loudly on the missing key, never silently
    propose against ref_id 0 (nm-se-merge.md's rename; prod had zero
    queued ``nm_propose`` rows when this shipped, so nothing legitimate
    still sends the old shape)."""
    store, ref_id = seeded_ctx
    ctx = _FakeCtx(store, ref_id, {"nm_ref_id": ref_id, "block": "hub"})
    sej._dispatch(ctx, sej.SPEC)
    assert ctx.status is None
    assert ctx.failure is not None and "malformed params" in ctx.failure
