"""gr514237 / gr514250 / gr514239 / gr514244 — the precis-workspace-help skill.

- the skill serves and ranks first for the workspace-build query;
- every ``precis-*-help`` slug cited in ``src/precis_se/handler.py``
  resolves to a shipped skill file;
- the stated ``t_`` rule and cad DSL frame conventions match the code
  that fixes them (``precis.cad.vec``/``scene``/``primitives``);
- the fenced ``import_workspace`` recipe names only real op keys.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.handlers.skill import SkillHandler, _list_skills

SKILL = "precis-workspace-help"
SKILL_FILE = (
    Path(__file__).resolve().parents[2] / "src/precis/data/skills" / f"{SKILL}.md"
)
HANDLER = Path(__file__).resolve().parents[2] / "src/precis_se/handler.py"

_FENCE_PY = re.compile(r"```python\n(.*?)```", re.DOTALL)


@pytest.fixture
def skill() -> SkillHandler:
    return SkillHandler(hub=Hub())


def test_skill_serves(skill: SkillHandler) -> None:
    out = skill.get(id=SKILL)
    assert SKILL in out.body
    assert len(out.body) > 100


def test_skill_search_ranks_first(skill: SkillHandler) -> None:
    """gr514237's failing query must now land on the skill first."""
    body = skill.search(q="build a macro mechanical design by script workspace").body
    rows = [
        ln.split()[0]
        for ln in body.splitlines()
        if ln.split()[:1] and ln.split()[0] in set(_list_skills())
    ]
    assert rows and rows[0] == SKILL, body


def test_handler_error_strings_cite_real_skills() -> None:
    """Every ``precis-*-help`` name in a handler string resolves."""
    cited = set(
        re.findall(
            r"precis-[a-z0-9]+(?:-[a-z0-9]+)*-help", HANDLER.read_text(encoding="utf-8")
        )
    )
    shipped = set(_list_skills())
    assert cited <= shipped, f"handler.py cites missing skills: {cited - shipped}"
    assert SKILL in cited  # _NO_TRACE cites it — the dead reference gr514237 hit


def test_no_trace_names_macro_limit_and_real_skill() -> None:
    """The 514244 message fix: the refusal names the macro-leg limit and
    a skill that exists."""
    text = HANDLER.read_text(encoding="utf-8")
    m = re.search(r"_NO_TRACE = \((.*?)\)\n", text, re.DOTALL)
    assert m is not None
    msg = "".join(re.findall(r'"([^"]*)"', m.group(1)))
    assert "macro" in msg and SKILL in msg


# ── conventions must match the code that fixes them ──────────────────


def test_skill_states_t_prefix_rule() -> None:
    body = SKILL_FILE.read_text(encoding="utf-8")
    assert "t_<part>" in body
    assert "one namespace" in body
    # the interim marker: template=True is NOT on this tree yet — the
    # skill must carry the gr514241 TODO, not a "from release" line
    assert "gr514241" in body and "template=True" in body


def test_rot_order_matches_vec() -> None:
    """The skill's R = Rz·Ry·Rx is the one convention ``vec.rotation``
    fixes — and z→−y under a +90° x rotation."""
    import numpy as np

    from precis.cad import vec

    body = SKILL_FILE.read_text(encoding="utf-8")
    assert "R = Rz·Ry·Rx" in body and "rotate about local x first" in body
    assert "Rz @ Ry @ Rx" in (vec.__doc__ or "") or "Rz @ Ry @ Rx" in (
        vec.rotation.__doc__ or ""
    )
    R = vec.rotation(np.pi / 2, 0.0, 0.0).R
    np.testing.assert_allclose(
        R @ np.array([0.0, 0.0, 1.0]), [0.0, -1.0, 0.0], atol=1e-12
    )


def test_cyl_box_sphere_conventions_match_primitives() -> None:
    from precis.cad.dsl import build, parse

    body = SKILL_FILE.read_text(encoding="utf-8")
    assert "base at `z=0`, axis **+z**" in body
    assert "centred in x/y, base at `z=0`" in body
    assert "centred at the origin" in body

    cyl = build(parse("cyl:r0.005h0.012"))
    lo, hi = cyl.aabb_local()
    assert lo[2] == 0.0 and hi[2] == pytest.approx(0.012)
    box = build(parse("box:w0.04d0.02h0.01"))
    lo, hi = box.aabb_local()
    assert lo[:2] == pytest.approx((-0.02, -0.01)) and lo[2] == 0.0
    sphere = build(parse("sphere:r0.006"))
    lo, hi = sphere.aabb_local()
    assert lo[2] == pytest.approx(-0.006)


def test_polar_is_about_the_node_frame_z() -> None:
    """``polar:nNrR`` fans copies at radius r about the node frame's z
    axis — ``@x,y`` is ignored, only ``@z`` survives (the convention
    gr514250 hit undocumented)."""
    import numpy as np

    from precis.cad.scene import _pattern_transforms, parse_source

    body = SKILL_FILE.read_text(encoding="utf-8")
    assert "**z axis**" in body and "(r·cos θ, r·sin θ, z)" in body
    spec = parse_source("spokes add box:w1mmd1mmh1mm @5mm,3mm,2mm polar:n4r5mm")
    (node,) = spec.nodes
    xfs = _pattern_transforms(node)
    assert len(xfs) == 4
    for xf, ang in zip(xfs, (0.0, np.pi / 2, np.pi, 3 * np.pi / 2), strict=True):
        np.testing.assert_allclose(
            xf.t, [5e-3 * np.cos(ang), 5e-3 * np.sin(ang), 2e-3], atol=1e-12
        )


def test_manifest_key_list_matches_the_vet() -> None:
    """The skill's key table is the real ``_BLOCK_KEYS`` & friends."""
    from precis_se import workspace_import as wi

    body = SKILL_FILE.read_text(encoding="utf-8")
    for key in wi._BLOCK_KEYS:
        assert f"`{key}`" in body, f"block key {key} undocumented"
    for key in wi._INSTANCE_KEYS:
        assert f"`{key}`" in body, f"instance key {key} undocumented"
    for key in wi._CONNECT_KEYS:
        assert f"`{key}`" in body, f"connect key {key} undocumented"
    # the "not yet supported" ruling (gr514245) must not drift
    for key in ("material", "measure", "binding", "bom"):
        assert f"`{key}`" in body


# ── the recipe matches the op it documents ───────────────────────────


def _recipe_block() -> str:
    blocks = _FENCE_PY.findall(SKILL_FILE.read_text(encoding="utf-8"))
    [recipe] = [b for b in blocks if "'op': 'import_workspace'" in b]
    return recipe


def test_skill_has_exactly_one_recipe_block() -> None:
    assert _recipe_block()


def test_recipe_states_only_real_op_keys() -> None:
    """The fenced op call names no key the vet would refuse — the
    nested ``run``/``files`` relpaths are not op keys (the regex's
    ``[a-z_]+`` skips every ``.``/``/``-carrying name)."""
    from precis_se import workspace_import as wi

    recipe = _recipe_block()
    for required in ("'op'", "'workspace'", "'run'"):
        assert required in recipe
    for key in re.findall(r"'([a-z_]+)':", recipe):
        assert key in wi._IMPORT_OP_KEYS, f"recipe key {key!r} not in the op"
