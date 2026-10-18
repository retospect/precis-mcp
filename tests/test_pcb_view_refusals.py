"""gr495607: pcb ``view=`` near-misses — one reading runs, two readings refuse."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.response import Response

_DESIGN = {
    "components": [
        {"refdes": "R1", "label": "10k", "part": "C25804", "pins": [{"name": "1"}]},
    ],
    "nets": [{"name": "VCC"}],
    "connections": [{"net": "VCC", "refdes": "R1", "pin": "1"}],
}


@pytest.fixture
def pcb(store):
    h = PcbHandler(hub=Hub(store=store))
    h.put(id="viewboard", args=_DESIGN)
    return h


def test_easyeda_reads_as_epro(
    pcb: PcbHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def fake(self, ref_id, view, args):
        seen.append(view)
        return Response(body="rendered")

    monkeypatch.setattr(PcbHandler, "_render_view", fake)
    resp = pcb.get(id="viewboard", view="easyeda")
    assert seen == ["epro"]
    assert resp.body == "interpreted: view='easyeda' read as view='epro'\nrendered"


@pytest.mark.parametrize("view", ["meta", "nets"])
def test_ambiguous_views_refuse_with_the_readings(pcb: PcbHandler, view: str) -> None:
    with pytest.raises(BadInput, match="unknown pcb view") as exc:
        pcb.get(id="viewboard", view=view)
    assert "omit view=" in (exc.value.next or "")
    assert "epro" in (exc.value.options or [])


def test_typo_gets_a_did_you_mean(pcb: PcbHandler) -> None:
    with pytest.raises(BadInput) as exc:
        pcb.get(id="viewboard", view="netlst")
    assert "view='netlist'" in (exc.value.next or "")
