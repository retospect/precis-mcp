"""Offline ``Board.check`` finding quality (gripe batch pcb-check):

gr521599 — findings carry ``pin`` and the declaring script line
(``site``); entities themselves keep the stamp (``part.site``,
``net.site``, ``pin.site``). gr521597 — ``pin.unknown`` names a wired
or function-tabled pin no known pad answers to. gr521598 —
``part.unknown``/``pin.unwired``. gr521603 — ``source=`` provenance.
gr521607 — ``over_voltage``. gr521604 — the check outcome lands in the
manifest's ``check`` block and ``run()`` echoes it. ``load_script``
execs a tmp ``main.py`` so a site assertion names the exact declaring
line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import precis_geom.pcb as pcb
from tests.workspaces._script import load_script


def test_findings_carry_pin_and_call_site(tmp_path: Path) -> None:
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C1")
u1.pin_functions({"1": {"functions": ["GPIO"], "max_current_a": 0.5}})
vcc = b.net("VCC", current=0.001, voltage=3.3)
vcc.tie(u1["1"].etype("input", fmax_hz=0.0, assume="test pin"), current=pcb.Max("2A"))
b.net("FLOATY", current=0.001)
b.part("U9", footprint="NOPE")
result = b.check()
""",
    )
    result = mod.result
    assert [f.code for f in result.findings] == [
        "footprint.unknown",
        "net.unconnected",
        "over_current",
    ]
    by_code = {f.code: f for f in result.findings}
    assert by_code["footprint.unknown"].site == "main.py:9"
    assert by_code["net.unconnected"].site == "main.py:8"
    oc = by_code["over_current"]
    assert (oc.refdes, oc.pin, oc.net) == ("U1", "1", "VCC")
    assert oc.site == "main.py:7"
    # The entity stamps the findings read off.
    assert mod.u1.site == "main.py:4"
    assert mod.vcc.site == "main.py:6"
    s = result.summary()
    assert "over_current U1.1:" in s
    assert "(main.py:7)" in s


def test_pin_unknown_names_typo_and_declaring_line(tmp_path: Path) -> None:
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
fp = pcb.Footprint("HDR", pads=[pcb.pad("1", "rect", 0, 0), pcb.pad("2", "rect", 1, 0)])
j1 = b.part("J1", footprint=fp)
vcc = b.net("VCC", current=0.001)
vcc.tie(j1["3"].etype("input", fmax_hz=0.0, assume="typo pin"))
gnd = b.net("GND", current=0.001)
gnd.tie(j1["1"].etype("input", fmax_hz=0.0, assume="hdr pin"))
u2 = b.part("U2", footprint=fp)
u2.pin_functions({"VBD": ["GPIO"]})
fp2 = pcb.Footprint(
    "SOT",
    pads=[pcb.pad("1", "rect", 0, 0), pcb.pad("2", "rect", 1, 0), pcb.pad("3", "rect", 2, 0)],
    pin_map={"1": "G", "2": "D", "3": "S"},
)
q1 = b.part("Q1", footprint=fp2)
gnd.tie(q1["G"].etype("input", fmax_hz=0.0, assume="fet gate"))
b.part("R1")
b.part("U9", footprint="GHOST")
result = b.check()
""",
    )
    result = mod.result
    assert [f.code for f in result.findings] == [
        "footprint.unknown",
        "pin.unknown",
        "pin.unknown",
    ]
    j1, u2 = result.findings[1], result.findings[2]
    assert (j1.refdes, j1.pin, j1.site) == ("J1", "3", "main.py:7")
    assert "footprint 'HDR' (2 pads)" in j1.message
    assert "known: 1, 2" in j1.message
    # A typo'd pin_functions key never mints a Pin — its site is the
    # pin_functions line.
    assert (u2.refdes, u2.pin, u2.site) == ("U2", "VBD", "main.py:11")
    # The pin_map-resolved name wires clean; lcsc-less and ghost-
    # footprint parts have no knowable universe and are skipped.
    assert result.not_checked == ["R1"]


def test_part_unknown_and_pin_unwired(tmp_path: Path) -> None:
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
other = pcb.Board("o")
u1 = b.part("U1", pins=["1", "2", "3", "4", "5", "6"])
r9 = other.part("R9", pins=["1", "2"])
vcc = b.net("VCC", current=0.001)
vcc.tie(u1["1"].etype("input", fmax_hz=0.0, assume="ic pin"))
vcc.tie(r9["1"].etype("input", fmax_hz=0.0, assume="foreign pin"))
u1["2"].nc()
u1["3"].must_terminate()
u1["4"].unused()
u1["5"].tag("nc")
u1["6"].meta["supplies"] = ["3v3"]
u2 = b.part("U2", pins=["A", "B"])
slot = u2.pin(any_of=["X", "Y"], name="SIG")
result = b.check()
""",
    )
    result = mod.result
    assert [f.code for f in result.findings] == [
        "part.unknown",
        "pin.unwired",
        "pin.unwired",
        "pin.unwired",
        "pin.unwired",
        "pin.unwired",
    ]
    pu = result.findings[0]
    assert (pu.refdes, pu.pin, pu.net, pu.site) == ("R9", "1", "VCC", "main.py:9")
    assert "never made" in pu.message
    mt, spare, slotf, ua, ub = result.findings[1:]
    assert (mt.refdes, mt.pin) == ("U1", "3")
    assert "must_terminate" in mt.message and "floating" in mt.message
    assert (spare.refdes, spare.pin) == ("U1", "6")
    assert "wire it, or mark it" in spare.message
    # One row for the untied slot — its candidates do not flag singly.
    assert (slotf.refdes, slotf.pin) == ("U2", "SIG")
    assert (ua.refdes, ua.pin) == ("U2", "A")
    assert (ub.refdes, ub.pin) == ("U2", "B")


def test_source_unparseable_and_unchecked_without_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delitem(sys.modules, "precis_client", raising=False)
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C1")
vcc = b.net("VCC", current=0.001)
vcc.tie(u1["1"].etype("input", fmax_hz=0.0, assume="ic pin"), source="ds:da1~dk1")
vcc.tie(u1["2"].etype("input", fmax_hz=0.0, assume="ic pin"), source="bogus")
result = b.check()
""",
    )
    result = mod.result
    assert [f.code for f in result.findings] == [
        "source.unresolvable",
        "source.unchecked",
    ]
    bad, unchecked = result.findings
    assert "does not parse" in bad.message and bad.site == "main.py:7"
    # One not-checked row covers every well-formed handle.
    assert unchecked.not_checked and "CANNOT be checked" in unchecked.message
    assert "source.unchecked [not-checked]" in result.summary()


def test_source_resolves_through_precis_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib

    class FakeClient:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        def call(self, verb: str, **kw: object) -> object:
            # Mimics the staged client's contract: tool errors arrive as
            # RuntimeError carrying the [error:<Kind>] envelope text.
            self.calls.append((verb, dict(kw)))
            if verb == "get":
                if str(kw["id"]).startswith("da404"):
                    raise RuntimeError(
                        "[error:NotFound] no such ref 'da404~dk0'\n"
                        "  next: check the datasheet id"
                    )
                return {"text": "chunk body"}
            if verb == "search":
                return (
                    "datasheet status — 1 of 1 ids resolved:\n\n"
                    "  da1  [vendorsheet]  full (9 body chunks)  pdf\n"
                    "      part C111  pull: ok\n"
                )
            raise AssertionError(verb)

    client = FakeClient()
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        return client if name == "precis_client" else real_import(name, package)

    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C222")
u1.pin_functions({"3": ["GPIO"]}, source="ds:da1~dk7")
vcc = b.net("VCC", current=0.001)
vcc.tie(u1["1"].etype("input", fmax_hz=0.0, assume="ic pin"), source="ds:da1~dk9")
vcc.tie(u1["2"].etype("input", fmax_hz=0.0, assume="ic pin"), source="ds:da404~dk0")
u2 = b.part("U2", lcsc="C111")
gnd = b.net("GND", current=0.001)
gnd.tie(u2["1"].etype("input", fmax_hz=0.0, assume="ic pin"), source="ds:da1~dk3")
""",
    )
    monkeypatch.setattr(importlib, "import_module", fake_import)
    result = mod.b.check()
    assert [f.code for f in result.findings] == [
        "source.wrong_part",
        "source.wrong_part",
        "source.unresolvable",
    ]
    pf, conn, missing = result.findings
    # The pin_functions handle names its own call line; da1 is C111's
    # sheet, claimed against C222.
    assert (pf.refdes, pf.site) == ("U1", "main.py:5")
    assert "C111" in pf.message and "C222" in pf.message
    assert (conn.refdes, conn.site) == ("U1", "main.py:7")
    assert "da404" in missing.message
    # Resolution is cached — a second check() makes no new client calls.
    n_calls = len(client.calls)
    mod.b.check()
    assert len(client.calls) == n_calls


def test_source_read_failures_classify_as_unchecked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr521603 — only a clear not-found reply is ``source.unresolvable``;
    a kind refusal, HTTP error, empty reply or timeout is a
    ``source.unchecked`` cannot-check row naming the reason (including a
    sandbox still running an older allowlist). The fake mimics the staged
    ``precis_client.call``: RuntimeError on every tool/transport error."""
    import importlib

    errors: dict[str, BaseException] = {
        "da404": RuntimeError("[error:NotFound] no such ref 'da404~dk0'"),
        "da500": RuntimeError(
            "[error:BadInput] get(kind='datasheet') is not served by the "
            "sandbox read-only MCP: it answers corpus and local kinds only, "
            "never live-fetch or paid kinds\n  options: calc, cad, …"
        ),
        "da501": RuntimeError("precis MCP HTTP 502: bad gateway"),
        "da502": RuntimeError("precis MCP: empty response"),
        "da503": TimeoutError("timed out"),
    }

    class FakeClient:
        def call(self, verb: str, **kw: object) -> object:
            if verb == "get":
                err = errors.get(str(kw["id"]).split("~")[0])
                if err is not None:
                    raise err
                return "chunk body"
            if verb == "search":
                return "datasheet status — 0 of 1 ids resolved"
            raise AssertionError(verb)

    client = FakeClient()
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        return client if name == "precis_client" else real_import(name, package)

    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C1")
n = b.net("N", current=0.001)
for i, ref in enumerate(("da404", "da500", "da501", "da502", "da503")):
    n.tie(
        u1[str(i + 1)].etype("input", fmax_hz=0.0, assume="ic pin"),
        source=f"ds:{ref}~dk0",
    )
""",
    )
    monkeypatch.setattr(importlib, "import_module", fake_import)
    result = mod.b.check()
    assert [f.code for f in result.findings] == [
        "source.unresolvable",
        "source.unchecked",
        "source.unchecked",
        "source.unchecked",
        "source.unchecked",
    ]
    missing, refused, http, empty, timeout = result.findings
    assert "da404" in missing.message and not missing.not_checked
    for f_ in (refused, http, empty, timeout):
        assert f_.not_checked and "CANNOT be checked" in f_.message
    assert "not served by the sandbox read-only MCP" in refused.message
    assert "HTTP 502" in http.message
    assert "empty response" in empty.message
    assert "timed out" in timeout.message


def test_source_unchecked_when_client_import_crashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``precis_client`` present but broken on import (not just
    absent) is still ``source.unchecked`` — never a leaked traceback."""
    import importlib

    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name == "precis_client":
            raise RuntimeError("staged client is corrupt")
        return real_import(name, package)

    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C1")
vcc = b.net("VCC", current=0.001)
vcc.tie(u1["1"].etype("input", fmax_hz=0.0, assume="ic pin"), source="ds:da1~dk1")
""",
    )
    monkeypatch.setattr(importlib, "import_module", fake_import)
    result = mod.b.check()
    assert [f.code for f in result.findings] == ["source.unchecked"]
    row = result.findings[0]
    assert row.not_checked and "CANNOT be checked" in row.message
    assert "failed to import" in row.message and "corrupt" in row.message


def test_over_voltage_and_never_guess_rows(tmp_path: Path) -> None:
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u1 = b.part("U1", lcsc="C1")
u1.pin_functions({"1": {"functions": ["GPIO"], "max_voltage_v": 5}})
hv = b.net("HV", current=0.001, voltage=20)
hv.tie(u1["1"].etype("input", fmax_hz=0.0, assume="hv pin"))
b.net_class("rail", working_voltage_v=48)
hv2 = b.net("HV2", current=0.001, cls="rail")
u3 = b.part("U3", lcsc="C3")
u3["1"].meta["max_voltage_v"] = 12
hv2.tie(u3["1"].etype("input", fmax_hz=0.0, assume="hv pin"))
sig = b.net("SIG", current=0.001)
u2 = b.part("U2", lcsc="C2")
u2["1"].meta["max_voltage_v"] = 3.3
sig.tie(u2["1"].etype("input", fmax_hz=0.0, assume="sig pin"))
result = b.check()
""",
    )
    result = mod.result
    assert [f.code for f in result.findings] == [
        "over_voltage",
        "over_voltage",
        "over_voltage",
    ]
    hv, hv2, sig = result.findings
    assert (hv.refdes, hv.pin, hv.site) == ("U1", "1", "main.py:7")
    assert "works at 20 V" in hv.message and "rated 5 V" in hv.message
    # Net-class working_voltage_v answers when the net itself doesn't.
    assert (hv2.refdes, hv2.pin, hv2.site) == ("U3", "1", "main.py:12")
    assert "works at 48 V" in hv2.message
    # The never-guess row: a bound pin on a voltage-less net asks the
    # user, marked not-checked, citing the net's declaring line.
    assert sig.not_checked and sig.site == "main.py:13"
    assert "voltage unknown for net SIG" in sig.message
    assert "U2/1" in sig.message


def test_over_voltage_silent_when_no_pin_bounds_exist(tmp_path: Path) -> None:
    """A board that never declares ``max_voltage_v`` gets no
    ``over_voltage`` row at all — clean boards stay clean (the server's
    rule-level vocabulary row would be a permanent un-actionable line
    here); only bound pins on voltage-less nets are cannot-check."""
    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
b.net("GND", current=0.001)
""",
    )
    result = mod.b.check()
    assert [f.code for f in result.findings] == ["net.unconnected"]


def test_manifest_records_check_and_run_echoes_it(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """gr521604 — ``manifest(check=...)`` carries the check outcome:
    ``findings``/``codes`` count real rows only, ``not_checked`` the
    cannot-check rows (unresolved parts + not-checked findings) with
    ``unverifiable`` the finding-level subset — a board with only
    cannot-check rows does not read as failing. The manifest on disk
    matches, and ``run()`` prints the same split. The kernel is faked
    missing so ``run()`` stays on the base-set path."""
    import importlib

    mod = load_script(
        tmp_path,
        """\
import precis_geom.pcb as pcb

b = pcb.Board("t")
u2 = b.part("U2", lcsc="C2")
u2["1"].meta["max_voltage_v"] = 3.3
sig = b.net("SIG", current=0.001)
sig.tie(u2["1"].etype("input", fmax_hz=0.0, assume="sig pin"))
b.net("FLOATY", current=0.001)
b.part("R1")
""",
    )
    b = mod.b
    # net.unconnected is the only real finding; the over_voltage
    # never-guess row and R1's unresolved part are cannot-check.
    expected = {
        "findings": 1,
        "codes": ["net.unconnected"],
        "not_checked": 2,
        "unverifiable": 1,
    }
    assert b.manifest(check=b.check())["check"] == expected
    real_import = importlib.import_module

    def fake_import(name: str, package: str | None = None) -> object:
        if name == "precis.pcb.local":
            raise ImportError("No module named 'precis.pcb.local'")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    out = pcb.run(b, tmp_path / "out", argv=[])
    printed = capsys.readouterr().out
    assert "check: 1 finding(s) (net.unconnected), 2 not-checked" in printed
    disk = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert disk["check"] == expected

    clean_out = pcb.run(pcb.Board("c"), tmp_path / "clean", argv=[])
    printed = capsys.readouterr().out
    assert "check: 0 finding(s)" in printed
    assert json.loads((clean_out / "manifest.json").read_text(encoding="utf-8"))[
        "check"
    ] == {"findings": 0, "codes": [], "not_checked": 0, "unverifiable": 0}


def test_manifest_check_none_never_runs_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``manifest(check=None)`` records ``"not run"`` — it must not run
    ``check()`` itself, since a ``source=`` resolution would be a hidden
    client round-trip."""

    def boom(self: pcb.Board) -> pcb.CheckResult:
        raise AssertionError("manifest() ran check()")

    monkeypatch.setattr(pcb.Board, "check", boom)
    man = pcb.Board("t").manifest()
    assert man["check"] == "not run"
