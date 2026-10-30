"""``precis_diag`` — core types, the Finding round trip, the renderer
golden, and the ``schema_version`` reader rule (accept older, refuse
newer with a next step)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import precis_diag
from hexfold.diag import finding_from_diag
from precis_diag import Anchor, Diag, FrameSite, RunResult, Status, render


def test_render_golden() -> None:
    """The one text renderer: status-first sort, the anchor's
    ``file:line (frame …)`` site, ``next:``/``refs:`` continuations."""
    diags = [
        Diag(status=Status.INFO, code="geom.summary", message="24 atoms"),
        Diag(
            status=Status.ERROR,
            code="cad.run",
            message="manifest.json: run failed — ValueError: rows must be >= 1",
            anchor=Anchor(frame_key="ab12cd34ef56", file="main.py", line=30),
            next="fix the script and re-run",
            refs=("block:tube",),
        ),
        Diag(
            status=Status.WARN,
            code="geom.clearance",
            message="no clearance rule for ('C','H')",
            where="sheet0",
        ),
        Diag(
            status=Status.NOT_CHECKED, code="drc.clearance", message="no board loaded"
        ),
    ]
    assert render(diags) == (
        "      error cad.run (main.py:30, frame ab12cd34ef56) "
        "manifest.json: run failed — ValueError: rows must be >= 1\n"
        "            next: fix the script and re-run\n"
        "            refs: block:tube\n"
        "       warn geom.clearance sheet0 no clearance rule for ('C','H')\n"
        "not_checked drc.clearance no board loaded\n"
        "       info geom.summary 24 atoms"
    )
    assert render([]) == "ok"


def test_finding_round_trip() -> None:
    """``finding_from_diag(diag_from_finding(f)) == f`` for every
    Finding field — Diag is the superset."""
    from hexfold.report import Finding, Severity

    f = Finding(
        code="geom.clash",
        severity=Severity.WARN,
        message="atoms closer than the bar",
        where="sheet0",
        span=(12, 4),
        data=(("measured", 1.79), ("expected", 1.8)),
        fix="widen the junction",
    )
    d = precis_diag.diag_from_finding(f)
    assert d.status is Status.WARN
    assert d.next == "widen the junction"
    assert d.anchor == Anchor()  # findings carry no frame anchor
    assert finding_from_diag(d) == f
    for sev in Severity:
        g = Finding(code="geom.check", severity=sev, message="m")
        assert finding_from_diag(precis_diag.diag_from_finding(g)) == g


def test_finding_from_diag_refuses_status_without_severity() -> None:
    for status in (Status.OK, Status.NOT_CHECKED):
        with pytest.raises(ValueError, match="no hexfold Severity"):
            finding_from_diag(Diag(status=status, code="geom.check", message="x"))


def test_status_mapping_table() -> None:
    """The docstring table: only info/warn/error have Finding or
    string-severity counterparts; ok and not_checked do not."""
    assert precis_diag._STATUS_FROM_SEVERITY_NAME == {
        "INFO": Status.INFO,
        "WARN": Status.WARN,
        "ERROR": Status.ERROR,
    }
    for status in (Status.INFO, Status.WARN, Status.ERROR):
        diag = Diag(status=status, code="geom.check", message="m")
        assert finding_from_diag(diag).severity.name == status.name


def test_dict_round_trip() -> None:
    d = Diag(
        status=Status.ERROR,
        message="run failed",
        code="cad.run",
        anchor=Anchor(frame_key="k", file="main.py", line=3),
        refs=("a", "b"),
        next="re-run",
        data=(("measured", 1.0),),
        where="w",
        span=(1, 2),
    )
    assert precis_diag.diag_from_dict(d.to_dict()) == d


def test_anchor_chain_round_trip_and_legacy_default() -> None:
    d = Diag(
        status=Status.ERROR,
        message="nested failure",
        code="cad.run-failed",
        anchor=Anchor(
            frame_key="outer",
            file="main.py",
            line=5,
            chain=(
                FrameSite("middle", "ops.py", 10),
                FrameSite("inner", "ops.py", 20),
            ),
        ),
    )
    raw = d.to_dict()
    assert raw["schema_version"] == 1
    assert raw["anchor"]["chain"] == [
        {"frame_key": "middle", "file": "ops.py", "line": 10},
        {"frame_key": "inner", "file": "ops.py", "line": 20},
    ]
    assert precis_diag.diag_from_dict(raw) == d
    del raw["anchor"]["chain"]
    assert precis_diag.diag_from_dict(raw).anchor.chain == ()


@pytest.mark.parametrize("chain", [{}, "inner", [None]])
def test_reader_refuses_malformed_chain(chain: object) -> None:
    with pytest.raises(ValueError, match="anchor.chain"):
        precis_diag.diag_from_dict({"status": "error", "anchor": {"chain": chain}})


def test_reader_accepts_older_refuses_newer() -> None:
    d = Diag(status=Status.OK, code="geom.check", message="clean").to_dict()
    assert precis_diag.diag_from_dict(d).status is Status.OK
    newer = {**d, "schema_version": precis_diag.SCHEMA_VERSION + 1}
    with pytest.raises(ValueError, match="newer.*upgrade precis-geom"):
        precis_diag.diag_from_dict(newer)
    with pytest.raises(ValueError, match="not in"):
        precis_diag.diag_from_dict({**d, "schema_version": 0})


def test_reader_preserves_original_schema_version(monkeypatch) -> None:
    monkeypatch.setattr(precis_diag, "SCHEMA_VERSIONS", frozenset({0, 1}))
    raw = {
        "schema_version": 0,
        "status": "info",
        "code": "geom.summary",
        "message": "older record",
    }
    diag = precis_diag.diag_from_dict(raw)
    assert diag.schema_version == 0
    assert diag.to_dict() == raw


@pytest.mark.parametrize(
    "code", ["", "run-failed", "CAD.run-failed", ".run", "cad.", "cad.run failed"]
)
def test_diag_refuses_unnamespaced_code(code: str) -> None:
    with pytest.raises(ValueError, match="lowercase domain"):
        Diag(status=Status.ERROR, code=code, message="bad code")
    with pytest.raises(ValueError, match="lowercase domain"):
        precis_diag.diag_from_dict({"status": "error", "code": code})


@pytest.mark.parametrize(
    "code", ["cad.run-failed", "geom.clash", "place.mirror_refused"]
)
def test_diag_accepts_namespaced_code(code: str) -> None:
    diag = Diag(status=Status.ERROR, code=code, message="valid code")
    assert precis_diag.diag_from_dict(diag.to_dict()) == diag


def test_run_result_round_trip_and_ok() -> None:
    rr = RunResult(
        diags=(Diag(status=Status.ERROR, code="cad.run-failed", message="boom"),),
        run="scratch/out/x/trace.json",
    )
    assert not rr.ok
    assert precis_diag.run_result_from_dict(rr.to_dict()) == rr
    assert RunResult().ok


def test_precis_diag_is_stdlib_only() -> None:
    """Every imported top-level module is stdlib or precis_diag itself,
    including imports nested in functions or conditional blocks."""
    import ast
    import sys
    from pathlib import Path

    root = Path(precis_diag.__file__).parent
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and node.level:
                    continue  # relative imports stay within precis_diag
                mods = (
                    [a.name for a in node.names]
                    if isinstance(node, ast.Import)
                    else [node.module or ""]
                )
                for mod in mods:
                    assert mod.split(".")[0] in sys.stdlib_module_names | {
                        "precis_diag"
                    }, f"{path.name}: import of {mod}"


# ── probe (S1c) ─────────────────────────────────────────────────────────


def _drop_resolvers(domain: str) -> None:
    """Drop one domain from the probe registry — test teardown only."""
    import sys

    mod = sys.modules["precis_diag.probe"]
    mod._REGISTRY[:] = [r for r in mod._REGISTRY if r.domain != domain]


class _ToyResolver:
    """The conformance shape every domain resolver follows: can_resolve,
    resolve, digest over a tiny in-memory entity set."""

    domain = "toy"

    def __init__(self, entities: dict[str, list[str]] | None = None) -> None:
        self._entities = entities or {
            "pins": ["U3.1", "U3.2", "U3.3"],
            "nets": ["net:A", "net:B"],
        }

    def can_resolve(self, selector: Any) -> bool:
        return isinstance(selector, str) and selector in self._entities

    def resolve(self, selector: Any) -> list[str]:
        if selector not in self._entities:
            raise ValueError(f"toy: unknown selector {selector!r}")
        return list(self._entities[selector])

    def digest(self, entities: list[str]) -> dict[str, Any]:
        kind = "nets" if entities and entities[0].startswith("net:") else "pins"
        return {"count": len(entities), "kind": kind, "ids": list(entities)}


@pytest.fixture()
def toy_resolver() -> Iterator[_ToyResolver]:
    """A registered toy resolver, dropped on teardown so the global
    registry never leaks between tests."""
    resolver = _ToyResolver()
    precis_diag.register_probe(resolver)
    yield resolver
    _drop_resolvers("toy")


def test_probe_returns_count_digest_sample(toy_resolver: _ToyResolver) -> None:
    result = precis_diag.probe("pins", label="u3")
    assert result["count"] == 3
    assert result["digest"] == {
        "count": 3,
        "kind": "pins",
        "ids": ["U3.1", "U3.2", "U3.3"],
    }
    assert result["sample"] == "U3.1, U3.2, U3.3"


def test_probe_sample_truncates_ids(toy_resolver: _ToyResolver) -> None:
    toy_resolver._entities["many"] = [f"x{i}" for i in range(10)]
    result = precis_diag.probe("many")
    assert result["count"] == 10
    assert result["sample"] == "x0, x1, x2 (+7 more)"


def test_probe_bbox_digest_samples_the_extent() -> None:
    class _BBox:
        domain = "bbox-toy"

        def can_resolve(self, selector: Any) -> bool:
            return selector == "box"

        def resolve(self, selector: Any) -> list[Any]:
            return [(0.0, 0.0), (4.0, 2.0)]

        def digest(self, entities: list[Any]) -> dict[str, Any]:
            xs = [p[0] for p in entities]
            ys = [p[1] for p in entities]
            return {
                "count": len(entities),
                "kind": "points",
                "bbox": [min(xs), min(ys), max(xs), max(ys)],
            }

    result = precis_diag.probe("box", resolver=_BBox())
    assert result["count"] == 2
    assert result["digest"]["bbox"] == [0.0, 0.0, 4.0, 2.0]
    assert result["sample"] == "[0, 0, 4, 2]"


def test_probe_sample_renders_a_3d_bbox() -> None:
    """The CAD resolver emits 6-number 3D extents — the sample must
    render them, not fall through to "" (gr522030 review round 1)."""

    class _BBox3D:
        domain = "bbox3d-toy"

        def can_resolve(self, selector: Any) -> bool:
            return selector == "box3"

        def resolve(self, selector: Any) -> list[Any]:
            return [(0.0, 0.0, 0.0), (1.0, 2.0, 3.0)]

        def digest(self, entities: list[Any]) -> dict[str, Any]:
            return {
                "count": len(entities),
                "kind": "points",
                "bbox": [0.0, 0.0, 0.0, 1.0, 2.0, 3.0],
            }

    result = precis_diag.probe("box3", resolver=_BBox3D())
    assert result["sample"] == "[0, 0, 0, 1, 2, 3]"


def test_probe_unresolvable_raises_probe_error(
    toy_resolver: _ToyResolver,
) -> None:
    with pytest.raises(precis_diag.ProbeError) as exc_info:
        precis_diag.probe("unowned")
    d = exc_info.value.diag
    assert d.code == "core.probe-unresolved"
    assert d.status is Status.ERROR
    assert d.next
    assert exc_info.value.diag_code == d.code
    assert exc_info.value.diag_refs == d.refs


def test_probe_resolve_raise_becomes_probe_error(
    toy_resolver: _ToyResolver,
) -> None:
    with pytest.raises(precis_diag.ProbeError) as exc_info:
        precis_diag.probe("pins", resolver=_RaisingResolver())
    assert exc_info.value.diag.code == "core.probe-unresolved"
    assert "boom" in exc_info.value.diag.message
    assert "nothing ran" in exc_info.value.diag.next


class _RaisingResolver:
    domain = "raising"

    def can_resolve(self, selector: Any) -> bool:
        return True

    def resolve(self, selector: Any) -> Any:
        raise ValueError("boom")

    def digest(self, entities: Any) -> dict[str, Any]:
        return {"count": 0, "kind": "none", "ids": []}


@pytest.mark.parametrize(
    "bad",
    [
        {"count": 1, "kind": "pins"},  # neither ids nor bbox
        {"count": 1, "kind": "pins", "ids": ["a"], "bbox": [0, 0, 1, 1]},  # both
        {"count": "1", "kind": "pins", "ids": ["a"]},  # count not int
        {"count": 1, "kind": "", "ids": ["a"]},  # empty kind
        {"count": 1, "kind": "pins", "ids": [1]},  # ids not str
        {"count": 1, "kind": "pins", "bbox": [0, 0, 1]},  # bbox not 4
    ],
)
def test_probe_malformed_digest_refuses(bad: dict[str, Any]) -> None:
    class _Bad:
        domain = "bad"

        def can_resolve(self, selector: Any) -> bool:
            return True

        def resolve(self, selector: Any) -> list[str]:
            return ["a"]

        def digest(self, entities: list[str]) -> dict[str, Any]:
            return dict(bad)

    with pytest.raises(precis_diag.ProbeError) as exc_info:
        precis_diag.probe("x", resolver=_Bad())
    assert exc_info.value.diag.code == "core.probe-digest"


def test_probe_register_replaces_same_domain() -> None:
    class _A(_ToyResolver):
        domain = "dup"

    precis_diag.register_probe(_A())
    replacement = _ToyResolver()
    replacement.domain = "dup"
    precis_diag.register_probe(replacement)
    domains = [r.domain for r in precis_diag.probe_resolvers()]
    assert domains.count("dup") == 1
    _drop_resolvers("dup")
