"""Stored-rate provenance and the browser's graph-filter contract (gr559349)."""

from __future__ import annotations

import json
import re
import subprocess
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from precis_web.pathway_filters import filter_kinetics_payload
from tests._node import needs_node

from .test_pathway_detail import _level_bars, _run_url_probe, _seed_pathway

_SCRIPT = Path("src/precis_web/static/pathway-filters.js")


def _run(tmp_path: Path, body: str) -> Any:
    script = tmp_path / "filters.js"
    script.write_text(
        _SCRIPT.read_text(encoding="utf-8") + "\nconst F = PathwayFilters;\n" + body,
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    return json.loads(result.stdout)


@needs_node
def test_thermal_ticks_and_temperature(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        "console.log(JSON.stringify([F.thermalTicks(298), F.thermalTicks(400)]));",
    )
    # Eyring constants give 0.579, 0.756, 0.967 eV at exactly 298 K;
    # the brief's 0.59, 0.77, 0.98 are approximate reference values.
    assert [t["eV"] for t in result[0]] == pytest.approx(
        [0.579, 0.756, 0.967], abs=0.001
    )
    assert [t["tau"] for t in result[0]] == [0.001, 1, 3600]
    assert all(b["eV"] > a["eV"] for a, b in zip(*result, strict=True))


_DAG = """
const graph = {
  nodes: ['R','A','B','C','D'].map(id => ({id, rel_energy:0})),
  links: [
    {source:'R',target:'A',barrier:1.4},
    {source:'R',target:'B',barrier:0.2},
    {source:'A',target:'C',barrier:0.1},
    {source:'B',target:'C',barrier:0.1},
    {source:'C',target:'D',barrier:0.1},
    {source:'C',target:'B',barrier:0.1},
  ],
  paths: [['R','A','C','D'], ['R','B','C','D']],
};
const layers = {barrier_filter:true, turnover_filter:true, bep:true};
const state = {barrier:0.77, turnover:-10};
"""


@needs_node
def test_surviving_route_merge_cut_glyph_and_cycle(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _DAG
        + """
const result = F.traverse(graph, layers, state, 298);
console.log(JSON.stringify({
  nodes: result.graph.nodes.map(n=>n.id), links:result.graph.links,
  routes:result.routeCuts,
  glyph:F.glyphs(result, {R:0}, {R:graph.nodes[0]}, x=>x, y=>y, 1),
}));
""",
    )
    assert result["nodes"] == ["R", "B", "C", "D"]
    assert len(result["links"]) == 4
    assert result["routes"][0]["label"] == "cut at R→A"
    assert result["routes"][1] is None
    assert 'class="pw-filter-cut"' in result["glyph"]
    assert "barrier 1.4 eV &gt; 0.77 eV" in result["glyph"]


@needs_node
def test_filter_preserves_renderer_columns_at_route_merge(
    client: Any, runtime: Any, tmp_path: Path
) -> None:
    graph = {
        "nodes": [{"id": n, "rel_energy": 0.0} for n in ("R", "A", "B", "C", "D")],
        "links": [
            {"source": "R", "target": "A", "barrier": 1.4},
            {"source": "R", "target": "B", "barrier": 0.1},
            {"source": "A", "target": "C", "barrier": 0.1},
            {"source": "B", "target": "C", "barrier": 0.1},
            {"source": "C", "target": "D", "barrier": 0.1},
        ],
        "paths": [["R", "A", "C", "D"], ["R", "B", "C", "D"]],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    html = client.get("/refs/pathway/171696").text
    original = _level_bars(_run_url_probe(tmp_path, html)["svg"])
    assert set(original) == {"R", "A", "B", "C", "D"}

    def slide(threshold: float) -> str:
        return _run_url_probe(
            tmp_path,
            html,
            after=f"""
els['pw-filter-barrier'].value = '{threshold}';
els['pw-filter-barrier'].handlers.input[0]();
""",
        )["svg"]

    svg = slide(0.77)
    surviving = _level_bars(svg)
    assert set(surviving) == {"R", "B", "C", "D"}
    for node, band in surviving.items():
        assert band[:2] == pytest.approx(original[node][:2])
    # Equal energies cannot hide an overlap through vertical separation.
    assert surviving["B"][2] == pytest.approx(surviving["C"][2])
    assert surviving["B"][1] < surviving["C"][0]
    connectors = {
        (source, target): points
        for source, target, points in re.findall(
            r'<polyline class="pw-seg"[^>]*data-from="([^"]*)" '
            r'data-to="([^"]*)"[^>]* points="([^"]*)"',
            svg,
        )
    }
    assert set(connectors) == {("R", "B"), ("B", "C"), ("C", "D")}
    xs = [float(point.split(",")[0]) for point in connectors["B", "C"].split()]
    assert xs[0] == pytest.approx(surviving["B"][1])
    assert xs[-1] == pytest.approx(surviving["C"][0])
    assert all(left < right for left, right in pairwise(xs))

    # Removing every suffix must not shrink the canvas and move the root.
    root_only = _level_bars(slide(0.05))
    assert set(root_only) == {"R"}
    assert root_only["R"][:2] == pytest.approx(original["R"][:2])


@needs_node
def test_turnover_cut_keeps_folded_supply_shoulder_and_cut_glyph(
    client: Any, runtime: Any, tmp_path: Path
) -> None:
    """gr559349: a cut reaction must not hide its reachable supply state."""
    graph = {
        "nodes": [
            {"id": "N", "rel_energy": 0.0},
            {"id": "N+H", "rel_energy": -0.2},
            {"id": "NH", "rel_energy": -0.4},
        ],
        "links": [
            {"source": "N", "target": "N+H", "kind": "supply"},
            {
                "source": "N+H",
                "target": "NH",
                "kind": "reaction",
                "barrier": 0.1,
            },
        ],
        "paths": [["N", "N+H", "NH"]],
    }
    _seed_pathway(
        runtime.store,
        meta={
            "graph": graph,
            "results": {
                "kinetics": {
                    "transitions": [{"from": "N+H", "to": "NH", "net_rate": 0}]
                }
            },
        },
        body_text=None,
    )
    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    cut_svg = _run_url_probe(tmp_path, response.text)["svg"]
    original_svg = _run_url_probe(
        tmp_path,
        response.text,
        after="""
els['pw-filter-turnover-on'].checked = false;
els['pw-filter-turnover-on'].handlers.change[0]();
""",
    )["svg"]
    shoulder_pattern = (
        r'<line class="pw-shoulder"[^>]*data-state="N\+H" '
        r'x1="([^"]*)" y1="([^"]*)" x2="([^"]*)" y2="([^"]*)"'
    )
    original_shoulder = re.findall(shoulder_pattern, original_svg)
    cut_shoulder = re.findall(shoulder_pattern, cut_svg)
    assert len(original_shoulder) == len(cut_shoulder) == 1
    assert tuple(map(float, cut_shoulder[0])) == pytest.approx(
        tuple(map(float, original_shoulder[0]))
    )
    assert 'class="pw-shoulder-drop"' in cut_svg
    assert set(
        re.findall(r'<rect class="pw-svg-node"[^>]*data-state="([^"]*)"', cut_svg)
    ) == {"N+H", "N"}
    assert 'class="pw-seg"' not in cut_svg
    assert 'data-from="N" data-to="NH"' in original_svg
    cuts = re.findall(
        r'<text class="pw-filter-cut" x="([^"]*)" y="([^"]*)"[^>]*>'
        r"<title>([^<]*)</title>⊣</text>",
        cut_svg,
    )
    assert len(cuts) == 1
    x, y, title = cuts[0]
    assert title.startswith("N+H→NH: termination: no turnover, 0 /s/site")
    assert float(x) == pytest.approx(float(cut_shoulder[0][2]) + 5)
    assert float(y) == pytest.approx(float(cut_shoulder[0][3]))
    assert 'class="pw-filter-cut"' not in original_svg
    assert _level_bars(cut_svg)["N"][:2] == pytest.approx(
        _level_bars(original_svg)["N"][:2]
    )


@needs_node
def test_hidden_supply_branch_cannot_move_surviving_shoulder_cut(
    client: Any, runtime: Any, tmp_path: Path
) -> None:
    """gr559349: the later hidden A→S branch overwrote C→S's cut position."""
    graph = {
        "nodes": [{"id": n, "rel_energy": 0.0} for n in ("R", "B", "C", "S", "P", "A")],
        "links": [
            {"source": "R", "target": "B", "barrier": 0.1},
            {"source": "B", "target": "C", "barrier": 0.1},
            {"source": "C", "target": "S", "kind": "supply", "barrier": 0.1},
            {"source": "S", "target": "P", "barrier": 1.4},
            {"source": "R", "target": "A", "barrier": 1.4},
            {"source": "A", "target": "S", "kind": "supply", "barrier": 0.1},
        ],
        "paths": [["R", "B", "C", "S", "P"], ["R", "A", "S", "P"]],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    svg = _run_url_probe(
        tmp_path,
        response.text,
        search="?layer_barrier_filter=1&filter_barrier=0.77",
    )["svg"]
    shoulders = re.findall(
        r'<line class="pw-shoulder"[^>]*data-state="S" '
        r'x1="[^"]*" y1="[^"]*" x2="([^"]*)" y2="([^"]*)"',
        svg,
    )
    assert len(shoulders) == 1
    cuts = re.findall(
        r'<text class="pw-filter-cut" x="([^"]*)" y="([^"]*)"[^>]*>'
        r"<title>S→P: barrier 1.4 eV &gt; 0.77 eV</title>⊣</text>",
        svg,
    )
    assert len(cuts) == 1
    assert float(cuts[0][0]) == pytest.approx(float(shoulders[0][0]) + 5)
    assert float(cuts[0][1]) == pytest.approx(float(shoulders[0][1]))
    assert set(_level_bars(svg)) == {"R", "B", "C"}


@needs_node
def test_reverse_adsorption_flux_cuts_forward_desorption(tmp_path: Path) -> None:
    """td563584: a stored reverse transition still supplies the forward rate."""
    result = _run(
        tmp_path,
        """
const graph = {
  nodes: ['NO@N', 'NH3', '*~nh3'].map(id => ({id, rel_energy:0})),
  links: [{source:'NO@N',target:'NH3'}, {source:'NH3',target:'*~nh3'}],
  paths: [['NO@N', 'NH3', '*~nh3']],
  filter_kinetics: [{source:'v1 chain net_rate', environment:{T:298},
    transitions:[{from:'*',to:'NH3',net_rate:-1e-100}]}],
};
const result = F.traverse(graph, {turnover_filter:true}, {turnover:-10}, 298);
console.log(JSON.stringify({nodes:result.graph.nodes.map(n=>n.id),
  cuts:result.cuts, routes:result.routeCuts,
  forward:result.rates.get(JSON.stringify(['NH3','*'])),
  reverse:result.rates.get(JSON.stringify(['*','NH3']))}));
""",
    )
    assert result["nodes"] == ["NO@N", "NH3"]
    assert len(result["cuts"]) == 1
    assert result["cuts"][0]["edge"] == {"source": "NH3", "target": "*~nh3"}
    assert result["cuts"][0]["reason"] == (
        "termination: no turnover, 1e-100 /s/site (v1 chain net_rate)"
    )
    assert result["routes"][0]["label"] == "cut at NH3→*~nh3"
    assert result["forward"]["value"] == pytest.approx(1e-100, abs=0)
    assert result["reverse"]["value"] == pytest.approx(-1e-100, abs=0)
    assert result["forward"]["environment"] == result["reverse"]["environment"]


@needs_node
def test_barrier_cut_does_not_turn_intermediate_into_desorbing_leaf(
    client: Any, runtime: Any, tmp_path: Path
) -> None:
    """td563584: filtering cannot change the preceding step's chemistry."""
    graph = {
        "nodes": [
            {"id": state, "rel_energy": 0.0} for state in ("NH2+H", "NH3", "*~nh3")
        ],
        "links": [
            {
                "source": "NH2+H",
                "target": "NH3",
                "kind": "reaction",
                "barrier": 0.1,
            },
            {
                "source": "NH3",
                "target": "*~nh3",
                "kind": "desorption",
                "barrier": 1.4,
            },
        ],
        "paths": [["NH2+H", "NH3", "*~nh3"]],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    original_svg = _run_url_probe(tmp_path, response.text)["svg"]
    assert "−NH₃ ↑" in original_svg
    cut = """
els['pw-filter-barrier'].value = '0.77';
els['pw-filter-barrier'].handlers.input[0]();
"""
    cut_svg = _run_url_probe(tmp_path, response.text, after=cut)["svg"]
    assert set(_level_bars(cut_svg)) == {"NH2+H", "NH3"}
    assert re.findall(
        r'<polyline class="pw-seg"[^>]*data-from="([^"]*)" data-to="([^"]*)"',
        cut_svg,
    ) == [("NH2+H", "NH3")]
    assert "NH3→*~nh3: barrier 1.4 eV &gt; 0.77 eV" in cut_svg
    assert "−NH₃ ↑" not in cut_svg
    restored_svg = _run_url_probe(
        tmp_path,
        response.text,
        after=cut
        + """
els['pw-filter-barrier-on'].checked = false;
els['pw-filter-barrier-on'].handlers.change[0]();
""",
    )["svg"]
    assert restored_svg == original_svg


@needs_node
def test_stored_flux_environment_includes_gas_composition(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
const stored = {T:298, p_total:1, U:0, pH:7, pressures:{NO:0.9,N2:0.1}};
const status = pressures => F.honesty(stored, {...stored, pressures});
const {pressures, ...unknownComposition} = stored;
console.log(JSON.stringify({
  same:status({N2:0.1,NO:0.9}),
  changed:status({NO:0.1,N2:0.9}),
  missing:status({NO:0.9}),
  extra:status({NO:0.9,N2:0.1,H2:0.2}),
  unknown:F.honesty(unknownComposition, stored),
  text:F.environmentText(stored),
}));
""",
    )
    assert result["same"] == "stored"
    for key in ("changed", "missing", "extra", "unknown"):
        assert result[key] == "not computed at the selected environment"
    assert result["text"] == (
        "flux at T = 298 K, 1 bar, p(NO) = 0.9 bar, p(N2) = 0.1 bar, "
        "U = 0 V RHE, pH = 7; re-solve to update"
    )


@needs_node
def test_unknown_bep_and_default_missing_rates(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        _DAG
        + """
graph.links[0].barrier = null;
const unknown = F.traverse(graph, layers, state, 298);
graph.links[0].bep = {alpha:0.5,beta:1}; graph.links[0].delta_e = 1;
const on = F.traverse(graph, layers, state, 298);
const off = F.traverse(graph, {...layers,bep:false}, state, 298);
const defaults = F.traverse(graph, {...layers,barrier_filter:false}, F.read(new URLSearchParams()), 298);
console.log(JSON.stringify({unknown:unknown.unknown, unknownCuts:unknown.cuts,
  on:on.cuts, off:off.cuts, unchanged:defaults.graph === graph}));
""",
    )
    assert len(result["unknown"]) == 1
    assert result["unknownCuts"] == []
    assert len(result["on"]) == 1
    assert result["off"] == []
    assert result["unchanged"] is True


@needs_node
def test_turnover_prefers_v2_per_edge_and_keeps_v1_environment(tmp_path: Path) -> None:
    records = filter_kinetics_payload(
        {
            "kinetics_v2": {
                "environment": {"T": 400, "gases": {"p_total": 2}},
                "transitions": [{"from": "R", "to": "A", "net_rate": 1}],
            },
            "kinetics": {
                "conditions": {"temperature": 298, "pressures": {"NO": 1}},
                "transitions": [
                    {"from": "R", "to": "A", "net_rate": 1e-100},
                    {"from": "R", "to": "B", "net_rate": 1e-100},
                ],
            },
        }
    )
    result = _run(
        tmp_path,
        _DAG
        + f"graph.filter_kinetics = {json.dumps(records)};\n"
        + """
const result = F.traverse(graph, {...layers,barrier_filter:false}, state, 298);
console.log(JSON.stringify({cuts:result.cuts, routes:result.routeCuts,
  rates:[result.rates.get(JSON.stringify(['R','A'])),
    result.rates.get(JSON.stringify(['R','B']))],
  text:F.environmentText(graph.filter_kinetics[1].environment),
  status:F.honesty(graph.filter_kinetics[1].environment,{T:400}),
}));
""",
    )
    assert len(result["cuts"]) == 1
    assert result["cuts"][0]["edge"]["target"] == "B"
    assert result["cuts"][0]["reason"] == (
        "termination: no turnover, 1e-100 /s/site (v1 chain net_rate)"
    )
    assert result["routes"][0] is None
    assert result["rates"][0]["source"] == "v2 per-step rate"
    assert result["rates"][0]["environment"]["T"] == pytest.approx(400)
    assert result["rates"][1]["environment"]["T"] == pytest.approx(298)
    assert result["text"] == "flux at T = 298 K, p(NO) = 1 bar; re-solve to update"
    assert result["status"] == "not computed at the selected environment"


@needs_node
def test_threshold_url_round_trip_and_invalid_values(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
const ignored=[], qs=new URLSearchParams('state=N%2BO&filter_barrier=1.4&filter_turnover=-8');
const state=F.read(qs,ignored); F.write(qs,state);
console.log(JSON.stringify({state,query:qs.toString(),restored:F.read(qs),
  invalid:F.read(new URLSearchParams('filter_barrier=&filter_turnover=Infinity'),ignored),ignored}));
""",
    )
    assert result["state"] == result["restored"] == {"barrier": 1.4, "turnover": -8}
    assert result["query"] == "state=N%2BO&filter_barrier=1.4&filter_turnover=-8"
    assert result["invalid"] == {"barrier": None, "turnover": -10}
    assert result["ignored"] == ["filter_barrier", "filter_turnover"]


@needs_node
def test_cut_stored_names_are_escaped(tmp_path: Path) -> None:
    result = _run(
        tmp_path,
        """
const name='<img src=x onerror="boom">';
const graph={nodes:[{id:name,rel_energy:0},{id:'T',rel_energy:0}],
 links:[{source:name,target:'T',barrier:2}],paths:[[name,'T']]};
const result=F.traverse(graph,{barrier_filter:true},{barrier:0.5,turnover:-10},298);
console.log(JSON.stringify(F.glyphs(result,{[name]:0},{[name]:graph.nodes[0]},x=>x,y=>y,1)));
""",
    )
    assert "<img" not in result
    assert "&lt;img src=x onerror=&quot;boom&quot;&gt;" in result


def test_missing_and_invalid_rates_are_not_zero_or_v2_tof() -> None:
    assert filter_kinetics_payload({"kinetics_v2": {"tof_mf": 0}}) == []
    rows = [
        {"from": "A", "to": "B", "net_rate": value}
        for value in (None, True, float("nan"), float("inf"), "0")
    ]
    assert filter_kinetics_payload({"kinetics": {"transitions": rows}}) == []
    records = filter_kinetics_payload(
        {"kinetics": {"transitions": [{"name": "A->B", "net_rate": 0}]}}
    )
    assert records[0]["transitions"] == [{"from": "A", "to": "B", "net_rate": 0}]


@needs_node
def test_filter_controls_restore_url_and_redraw_routes(
    client: Any, runtime: Any, tmp_path: Path
) -> None:
    graph = {
        "nodes": [{"id": n, "rel_energy": 0.0} for n in ("R", "A", "B")],
        "links": [
            {"source": "R", "target": "A", "barrier": 0.9},
            {"source": "R", "target": "B", "barrier": 0.1},
        ],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    html = client.get("/refs/pathway/171696").text
    assert 'id="pw-filter-controls"' in html
    assert 'src="/static/pathway-filters.js"' in html
    default = _run_url_probe(tmp_path, html)
    assert 'data-to="A"' in default["svg"]
    assert 'class="pw-filter-cut"' not in default["svg"]

    after = """
els['pw-filter-barrier-on'].checked = true;
els['pw-filter-barrier-on'].handlers.change[0]();
if (!created.some(b => b.classList.contains('pw-path-btn') && b.innerHTML.includes('cut at R→A'))) {
  throw new Error('route picker did not mark the cut');
}
if (!els['pw-filter-rates'].textContent.includes('No stored per-step rates')) {
  throw new Error('missing rates were not explained');
}
"""
    enabled = _run_url_probe(tmp_path, html, after=after)
    assert 'data-to="A"' not in enabled["svg"]
    assert 'class="pw-filter-cut"' in enabled["svg"]
    assert "layer_barrier_filter=1" in enabled["rs"][-1]
    query = enabled["rs"][-1].split("?", 1)[1]
    restored = _run_url_probe(tmp_path, html, search="?" + query)
    assert restored["svg"] == enabled["svg"]

    warmer = _run_url_probe(
        tmp_path,
        html,
        search="?" + query,
        after="""
docHandlers['pw-environment-change'][0]({detail:{T:400}});
if (created.some(b => b.classList.contains('pw-path-btn') && b.innerHTML.includes('cut at'))) {
  throw new Error('route mark did not clear when the thermal threshold moved');
}
""",
    )
    assert 'data-to="A"' in warmer["svg"]
    assert 'class="pw-filter-cut"' not in warmer["svg"]


@needs_node
def test_default_turnover_cuts_zero_and_negative_flux_but_not_missing(
    tmp_path: Path,
) -> None:
    result = _run(
        tmp_path,
        _DAG
        + """
graph.filter_kinetics = [{source:'v1 chain net_rate',transitions:[
  {from:'R~history',to:'A',net_rate:0}, {from:'R',to:'B',net_rate:-2}
]}];
const result=F.traverse(graph,{...layers,barrier_filter:false},state,298);
console.log(JSON.stringify({nodes:result.graph.nodes.map(n=>n.id),cuts:result.cuts}));
""",
    )
    assert result["nodes"] == ["R"]
    assert len(result["cuts"]) == 2


@needs_node
@pytest.mark.parametrize(
    "action",
    [
        "els['pw-ph-input'].value = '7'; els['pw-ph-input'].handlers.input[0]();",
        "els['pw-filter-barrier-on'].checked = true; "
        "els['pw-filter-barrier-on'].handlers.change[0]();",
        "els['pw-filter-turnover-on'].checked = false; "
        "els['pw-filter-turnover-on'].handlers.change[0]();",
        "els['pw-filter-barrier'].value = '0.77'; "
        "els['pw-filter-barrier'].handlers.input[0]();",
        "els['pw-filter-turnover'].value = '-8'; "
        "els['pw-filter-turnover'].handlers.input[0]();",
        "els['pw-filter-tick-buttons'].children[0].handlers.click[0]();",
        "docHandlers['pw-environment-change'][0]({detail:{T:400}});",
    ],
    ids=[
        "ph",
        "barrier-toggle",
        "turnover-toggle",
        "barrier",
        "turnover",
        "tick",
        "environment",
    ],
)
def test_redraw_preserves_selected_state_highlight(
    client: Any, runtime: Any, tmp_path: Path, action: str
) -> None:
    """gr559349: controls must keep the SVG outline promised by the readout."""
    graph = {
        "nodes": [{"id": n, "rel_energy": 0.0} for n in ("R", "A")],
        "links": [{"source": "R", "target": "A", "barrier": 1.4}],
        "paths": [["R", "A"]],
    }
    _seed_pathway(runtime.store, meta={"graph": graph}, body_text=None)
    response = client.get("/refs/pathway/171696")
    assert response.status_code == 200
    result = _run_url_probe(tmp_path, response.text, search="?state=R", after=action)
    assert 'Selected state <b class="font-mono">R</b>' in result["sel"]
    assert "outlined in the diagram" in result["sel"]
    selected = {
        rect["class"]: rect
        for rect in result["diagramRects"]
        if rect.get("data-state") == "R"
    }
    assert selected["pw-svg-node"]["stroke"] == "#0284c7"
    assert selected["pw-svg-node"]["stroke-width"] == "2"
    assert selected["pw-svg-colbg"]["fill"] == "#f0f9ff"
    assert selected["pw-svg-colbg"]["stroke"] == "#bae6fd"
