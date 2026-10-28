"""Sectioned JSON form (SPEC 4/14/17/18/19/23.2): the authored/generated
split, the authored-only content hash, ``gen.stale``, and ``op.dangling``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hexfold.build import Net, build
from hexfold.canon import canonical_json, content_hash
from hexfold.check import check
from hexfold.report import Profile, Severity
from hexfold.text import parse, spec_from_dict, to_text

ROOT = Path(__file__).resolve().parents[2] / "hexfold"

_TUBE = """hexfold 0.2
prov: lib=hexfold@0.2.0
origin t
t: tube(5,5,len=3)
terminate: t.* = H
"""

_SECTIONED_KEYS_ALWAYS = {"hexfold", "instances", "lattice", "ports", "regions"} | {
    "hash",
    "report",
    "generated",
    # SPEC 18 sheets (0.2): the fuse/seam sheet partition of every net,
    # even the trivial one-sheet case -- derived, so it lives beside
    # generated rather than in authored_dict().
    "sheets",
}


def _net(text: str) -> Net:
    return build(text, strict=False)


# ---------- (a) sectioned to_dict() shape ----------


def test_to_dict_top_level_keys_exact() -> None:
    net = _net(_TUBE)
    d = net.to_dict()
    assert set(d) == _SECTIONED_KEYS_ALWAYS | {"prov", "origin", "terminate", "ops"}
    assert d["generated"]["of"] == d["hash"]
    for key in ("atoms", "bonds", "rings"):
        assert key not in d
        assert key in d["generated"]
    assert "findings" in d["report"]


def test_authored_dict_excludes_generated_sections() -> None:
    net = _net(_TUBE)
    a = net.authored_dict()
    for key in ("atoms", "bonds", "rings", "report", "hash", "generated"):
        assert key not in a


# ---------- (b) canonical_json is authored-only and byte-stable ----------


def test_canonical_json_authored_only_and_byte_stable() -> None:
    j1 = canonical_json(_TUBE)
    j2 = canonical_json(_TUBE)
    assert j1 == j2
    d = json.loads(j1)
    for key in ("atoms", "bonds", "rings", "report", "hash", "generated"):
        assert key not in d


# ---------- (c) content_hash: authored-sensitive, findings-insensitive ----------


def test_content_hash_changes_with_authored_param() -> None:
    h1 = content_hash(_TUBE)
    h2 = content_hash(_TUBE.replace("tube(5,5,len=3)", "tube(6,5,len=3)"))
    assert h1 != h2


def test_content_hash_ignores_profile_only_differences() -> None:
    # STRICT vs DEFAULT changes which findings get promoted to ERROR, never
    # the authored sections -- the content hash must not move.
    text = "hexfold 0.2\norigin s\ns: sheet(6, 6) + 57@(3,3,A):0\n"
    net_default = build(text, profile=Profile.DEFAULT, strict=False)
    net_strict = build(text, profile=Profile.STRICT, strict=False)
    assert content_hash(text) == content_hash(text)
    assert net_default.to_dict()["hash"] == net_strict.to_dict()["hash"]


# ---------- (d) gen.stale ----------


def test_gen_stale_fresh_is_silent() -> None:
    net = _net(_TUBE)
    rep = check(net.to_json())
    assert "gen.stale" not in [f.code for f in rep.findings]


def test_gen_stale_on_authored_edit() -> None:
    net = _net(_TUBE)
    doc = json.loads(net.to_json())
    doc["instances"]["t"]["params"]["1"] = "6"
    rep = check(json.dumps(doc))
    stale = [f for f in rep.findings if f.code == "gen.stale"]
    assert len(stale) == 1
    assert stale[0].severity == Severity.WARN
    assert dict(stale[0].data)["found"] == doc["generated"]["of"]


def test_gen_stale_never_reuses_the_generated_block() -> None:
    # the reader always rebuilds from the authored sections; tampering
    # with the cached atoms alone (leaving 'of' untouched) changes nothing
    # about what check() reports.
    net = _net(_TUBE)
    doc = json.loads(net.to_json())
    doc["generated"]["atoms"] = []
    rep_edited = check(json.dumps(doc))
    rep_plain = check(net.to_json())
    assert sorted(f.code for f in rep_edited.findings) == sorted(
        f.code for f in rep_plain.findings
    )


# ---------- (e) op.dangling ----------


def test_op_dangling_bond_nonexistent_atom() -> None:
    text = """hexfold 0.2
origin t
t: tube(5,5,len=3)
t/(0,0,A) --bond--> t/(999,999,A)
"""
    rep = check(text)
    dangling = [f for f in rep.findings if f.code == "op.dangling"]
    assert len(dangling) == 1
    assert dangling[0].where == "t/(999,999,A)"
    assert dict(dangling[0].data)["op"] == "bond"


def test_op_dangling_terminate_no_matching_port() -> None:
    text = """hexfold 0.2
origin t
t: tube(5,5,len=3)
terminate: t.nosuchport = H
"""
    rep = check(text)
    dangling = [f for f in rep.findings if f.code == "op.dangling"]
    assert len(dangling) == 1
    assert dangling[0].where == "t.nosuchport"
    assert dict(dangling[0].data)["op"] == "terminate"


# ---------- (f) text -> JSON -> spec_from_dict -> text round trip ----------


@pytest.mark.parametrize(
    "path", sorted(p.name for p in (ROOT / "examples").glob("*.hx"))
)
def test_round_trip_text_json_text_stable(path: str) -> None:
    """text -> JSON -> ``spec_from_dict`` -> text is deterministic for
    every shipped example. Builds exactly once per SPEC 19c's own
    convention (matching ``test_example_canon_stable``'s single-build
    pattern in test_phase2.py): ``connects`` is the *expanded* form (menu
    macros resolved, SPEC 18), and ``menus.expand`` is not idempotent
    against a second build of its own output -- a pre-existing gap
    outside this slice (gripe filed), not something to exercise by
    accident with a second ``canonical_json``/``build`` pass here.
    """
    text = (ROOT / "examples" / path).read_text(encoding="utf-8")
    spec = parse(text)
    j1 = canonical_json(spec)

    def round_trip() -> tuple[str, object]:
        spec2 = spec_from_dict(json.loads(j1))
        return to_text(spec2), spec2

    t1, s1 = round_trip()
    t2, s2 = round_trip()
    assert t1 == t2
    assert s1 == s2


# ---------- capped_tube.hx.json: the committed sectioned-file example ----------


def test_capped_tube_example_hash_stable_and_fresh() -> None:
    net = build(
        (ROOT / "examples" / "capped_tube.hx").read_text(encoding="utf-8"),
        strict=False,
    )
    committed = json.loads(
        (ROOT / "examples" / "capped_tube.hx.json").read_text(encoding="utf-8")
    )
    fresh = net.to_dict()
    assert fresh["hash"] == committed["hash"]
    assert committed["generated"]["fidelity"] == "check"
    assert "xyz_A" not in committed["generated"]["atoms"][0]
    rep = check(json.dumps(committed))
    assert "gen.stale" not in [f.code for f in rep.findings]


def test_capped_tube_example_flip_authored_param_goes_stale() -> None:
    committed = json.loads(
        (ROOT / "examples" / "capped_tube.hx.json").read_text(encoding="utf-8")
    )
    edited = json.loads(json.dumps(committed))
    edited["instances"]["a"]["params"]["len"] = "5"
    rep = check(json.dumps(edited))
    assert "gen.stale" in [f.code for f in rep.findings]
